"""A small JSON Schema checker for the tool arguments (TOOLS.md T1), and the constants
that bound one library walk.

Only the keywords the Engagements schemas use are supported: ``type``, ``enum``,
``const``, ``minLength``, ``maxLength``, ``pattern``, ``minimum``, ``maximum``,
``minItems``, ``maxItems``, ``required``, ``properties``, ``additionalProperties``
and ``items``. ``validate`` collects every failure rather than stopping at the
first, in schema order, so the caller can report the first one deterministically
(TOOLS.md "Error precedence": fields are checked in the schema's property order).

Standard library only. It imports nothing from Hermes.
"""

import re
from dataclasses import dataclass

# The traversal budget for one library walk.
# The walk counts every directory entry it lists, whatever that entry is, so a
# library that points at the whole of a large notes vault is bounded by the same
# two numbers as one that holds records only. At or below ``WALK_ENTRY_WARNING``
# nothing is said; above it ``doctor`` warns that the root is very large; above
# ``WALK_ENTRY_LIMIT`` the walk stops and discovery is marked incomplete, because
# an inventory it did not finish cannot prove an id is free or that nothing
# similar exists. There is no time-based budget: results would then depend on
# machine load.
WALK_ENTRY_WARNING = 20_000
WALK_ENTRY_LIMIT = 200_000

MISSING = "missing_field"
UNEXPECTED = "unexpected_field"
INVALID = "invalid_value"


@dataclass(frozen=True)
class ValidationFailure:
    """One failing field. ``path`` is dotted for objects and indexed for arrays."""

    path: str
    reason: str


def _is_type(value, name):
    if name == "string":
        return isinstance(value, str)
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if name == "boolean":
        return isinstance(value, bool)
    if name == "object":
        return isinstance(value, dict)
    if name == "array":
        return isinstance(value, list)
    if name == "null":
        return value is None
    return False


def _matches(pattern, value):
    # ``$`` must mean the end of the string: Python's ``$`` also matches before a
    # trailing newline, which would let "acme\n" through an id pattern.
    return re.search(pattern.replace("$", r"\Z") if pattern.endswith("$") else pattern, value) is not None


def _join(path, name):
    return f"{path}.{name}" if path else name


def _check(value, schema, path, out):
    """Append the failures of ``value`` against ``schema`` to ``out``; return nothing."""
    expected = schema.get("type")
    if expected is not None and not _is_type(value, expected):
        out.append(ValidationFailure(path, INVALID))
        return
    if "const" in schema and (value != schema["const"] or type(value) is not type(schema["const"])):
        out.append(ValidationFailure(path, INVALID))
        return
    if "enum" in schema and not any(value == item and type(value) is type(item) for item in schema["enum"]):
        out.append(ValidationFailure(path, INVALID))
        return
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", len(value)):
            out.append(ValidationFailure(path, INVALID))
            return
        pattern = schema.get("pattern")
        if pattern is not None and not _matches(pattern, value):
            out.append(ValidationFailure(path, INVALID))
            return
    if _is_type(value, "number"):
        if "minimum" in schema and value < schema["minimum"]:
            out.append(ValidationFailure(path, INVALID))
            return
        if "maximum" in schema and value > schema["maximum"]:
            out.append(ValidationFailure(path, INVALID))
            return
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", len(value)):
            out.append(ValidationFailure(path, INVALID))
            return
        items = schema.get("items")
        if items is not None:
            for index, item in enumerate(value):
                # A scalar element reports at the array's own name; an object
                # element at its index (TOOLS.md stage-1 field naming).
                child = f"{path}[{index}]" if isinstance(item, dict) else path
                _check(item, items, child, out)
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        required = schema.get("required", ())
        for name, sub in properties.items():
            if name in value:
                _check(value[name], sub, _join(path, name), out)
            elif name in required:
                out.append(ValidationFailure(_join(path, name), MISSING))
        for name in required:
            if name not in properties and name not in value:
                out.append(ValidationFailure(_join(path, name), MISSING))
        if schema.get("additionalProperties") is False:
            for name in value:
                if name not in properties:
                    out.append(ValidationFailure(_join(path, str(name)), UNEXPECTED))


def validate(instance, schema):
    """Every failure of ``instance`` against ``schema``; an empty list means valid."""
    out = []
    _check(instance, schema, "", out)
    return out
