"""Pin the tool and op schemas to docs/TOOLS.md, and the validator to its keyword contract."""

import json
import re
from pathlib import Path

import pytest
from engagements.errors import EngagementsError
from engagements.records import validate_shapes

DOC = Path(__file__).resolve().parent.parent / "docs" / "TOOLS.md"
TOOL_NAMES = ("engagements_find", "engagements_bind", "engagements_context", "engagements_create",
              "engagements_update")
COMPOSITION = {"oneOf", "anyOf", "allOf"}


# --- reading the normative markdown -------------------------------------------


def _sections(text):
    parts = re.split(r"(?m)^## ", text)
    found = {}
    for part in parts[1:]:
        heading, _, body = part.partition("\n")
        found[heading.strip()] = body
    return found


def _json_blocks(text):
    return [json.loads(block) for block in re.findall(r"```json\n(.*?)\n```", text, re.S)]


def _doc_parameters(tool):
    body = _sections(DOC.read_text(encoding="utf-8"))[f"`{tool}`"]
    if tool == "engagements_update":
        body = body.split("### Model-facing schema", 1)[1]
    return _json_blocks(body)[0]


def _doc_op_schemas():
    body = _sections(DOC.read_text(encoding="utf-8"))["`engagements_update`"]
    return _json_blocks(body.split("### Per-op validation schemas", 1)[1])[0]


def _doc_descriptions():
    body = _sections(DOC.read_text(encoding="utf-8"))["Tool descriptions (model-facing)"]
    table = {}
    for line in body.splitlines():
        if not line.startswith("| `engagements_"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        table[cells[0].strip("`")] = cells[1]
    return table


def _composition_keys(value, path=""):
    if isinstance(value, dict):
        for key, item in value.items():
            yield path, key
            yield from _composition_keys(item, f"{path}/{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _composition_keys(item, f"{path}/{index}")


# --- the schemas themselves ---------------------------------------------------


def test_tool_schemas_has_exactly_the_five_tools():
    from engagements.tools import TOOL_SCHEMAS

    assert len(TOOL_SCHEMAS) == 5
    assert set(TOOL_SCHEMAS) == set(TOOL_NAMES)
    for name, schema in TOOL_SCHEMAS.items():
        assert set(schema) == {"name", "description", "parameters"}
        assert schema["name"] == name
        assert isinstance(schema["description"], str) and schema["description"].strip()
        assert isinstance(schema["parameters"], dict)


@pytest.mark.parametrize("tool", TOOL_NAMES)
def test_parameters_match_the_documented_json_block(tool):
    from engagements.tools import TOOL_SCHEMAS

    assert TOOL_SCHEMAS[tool]["parameters"] == _doc_parameters(tool)


def test_op_schemas_match_the_documented_block():
    from engagements.tools import OP_SCHEMAS

    assert _doc_op_schemas() == OP_SCHEMAS


def test_descriptions_are_the_documented_table():
    from engagements.tools import TOOL_SCHEMAS

    assert {name: schema["description"] for name, schema in TOOL_SCHEMAS.items()} == _doc_descriptions()


def _first_sentence(description):
    return description.split(". ", 1)[0].strip()


@pytest.mark.parametrize("tool", TOOL_NAMES)
def test_first_sentence_is_short_and_leads_with_a_searchable_word(tool):
    from engagements.tools import TOOL_SCHEMAS

    sentence = _first_sentence(TOOL_SCHEMAS[tool]["description"])
    assert len(sentence) <= 60
    assert any(word in sentence.casefold() for word in ("client", "project", "engagement"))


@pytest.mark.parametrize("tool", TOOL_NAMES)
def test_no_schema_uses_a_composition_keyword(tool):
    from engagements.tools import TOOL_SCHEMAS

    offenders = [(path, key) for path, key in _composition_keys(TOOL_SCHEMAS[tool]["parameters"])
                 if key in COMPOSITION]
    assert offenders == []


def test_no_op_schema_uses_a_composition_keyword():
    from engagements.tools import OP_SCHEMAS

    offenders = [(path, key) for path, key in _composition_keys(OP_SCHEMAS) if key in COMPOSITION]
    assert offenders == []


# --- stage 1 shape validation versus the per-op schemas -----------------------

SAMPLES = [
    ("status", {"op": "set_property", "key": "status", "value": "paused"}),
    ("name", {"op": "set_property", "key": "name", "value": "Acme Bakery"}),
    ("client", {"op": "set_property", "key": "client", "value": "riverbend-dental"}),
    ("aliases", {"op": "set_property", "key": "aliases", "values": ["Acme", "AB"]}),
    ("locations", {"op": "set_property", "key": "locations", "values": ["~/clients/acme/docs"]}),
    ("empty-aliases", {"op": "set_property", "key": "aliases", "values": []}),
    ("empty-name", {"op": "set_property", "key": "name", "value": ""}),
    ("long-name", {"op": "set_property", "key": "name", "value": "x" * 201}),
    ("missing-value", {"op": "set_property", "key": "name"}),
    ("wrong-field-for-list", {"op": "set_property", "key": "aliases", "value": "Acme"}),
    ("empty-alias", {"op": "set_property", "key": "aliases", "values": [""]}),
    ("numeric-status", {"op": "set_property", "key": "status", "value": 5}),
    ("string-for-values", {"op": "set_property", "key": "aliases", "values": "Acme"}),
    ("replace-valid", {"op": "replace_section", "section": "Contacts", "body": "- Jane Rivera"}),
    ("replace-missing-body", {"op": "replace_section", "section": "Contacts"}),
    ("replace-long-body", {"op": "replace_section", "section": "Contacts", "body": "x" * 20001}),
    ("replace-empty-section", {"op": "replace_section", "section": "", "body": "x"}),
    ("replace-extra-field", {"op": "replace_section", "section": "Contacts", "body": "x", "extra": 1}),
    ("append-valid", {"op": "append_to_section", "section": "Notes", "text": "hi"}),
    ("append-long-text", {"op": "append_to_section", "section": "Notes", "text": "x" * 20001}),
    ("append-boolean", {"op": "append_to_section", "section": "Notes", "text": True}),
    ("line-valid", {"op": "replace_line", "section": "Contacts", "match": "jane", "text": "- J"}),
    ("line-missing-text", {"op": "replace_line", "section": "Contacts", "match": "jane"}),
    ("line-empty-match", {"op": "replace_line", "section": "Contacts", "match": "", "text": "- J"}),
    ("remove-valid", {"op": "remove_line", "section": "Contacts", "match": "jane"}),
    ("remove-long-match", {"op": "remove_line", "section": "Contacts", "match": "x" * 1001}),
    ("line-long-text", {"op": "replace_line", "section": "Contacts", "match": "jane", "text": "x" * 1001}),
    ("long-section", {"op": "replace_section", "section": "x" * 201, "body": ""}),
    ("remove-extra-field", {"op": "remove_line", "section": "Contacts", "match": "jane", "text": "x"}),
]


def _op_schema(op):
    from engagements.tools import OP_SCHEMAS

    name = op["op"]
    if name == "set_property":
        if op.get("key") in ("aliases", "locations"):
            return OP_SCHEMAS["set_property_list"]
        return OP_SCHEMAS["set_property_scalar"]
    return OP_SCHEMAS[name]


@pytest.mark.parametrize("case,op", SAMPLES, ids=[case for case, _ in SAMPLES])
def test_shape_validation_agrees_with_the_op_schema(case, op):
    from engagements.schema import validate

    valid = not validate(op, _op_schema(op))
    try:
        validate_shapes([op])
    except EngagementsError as exc:
        assert exc.code == "invalid_op"
        assert valid is False, f"{case}: shape validation rejected an op the schema accepts"
    else:
        assert valid is True, f"{case}: shape validation accepted an op the schema rejects"


# --- the validator's keyword contract ----------------------------------------


def _failures(instance, schema):
    from engagements.schema import validate

    return validate(instance, schema)


def _pairs(failures):
    return [(failure.path, failure.reason) for failure in failures]


def _reasons(failures):
    return [failure.reason for failure in failures]


def test_validate_accepts_a_valid_instance():
    schema = {"type": "object", "properties": {"status": {"type": "string", "enum": ["active"]}},
              "required": ["status"], "additionalProperties": False}
    assert _failures({"status": "active"}, schema) == []


def test_type_keyword():
    assert _reasons(_failures(1, {"type": "string"})) == ["invalid_value"]
    assert _failures("1", {"type": "string"}) == []
    assert _failures(["a"], {"type": "array"}) == []


def test_a_boolean_is_not_an_integer():
    assert _reasons(_failures(True, {"type": "integer"})) == ["invalid_value"]
    assert _failures(True, {"type": "boolean"}) == []
    assert _failures(1, {"type": "integer"}) == []


def test_enum_and_const():
    assert _reasons(_failures("bogus", {"enum": ["a", "b"]})) == ["invalid_value"]
    assert _failures("b", {"enum": ["a", "b"]}) == []
    assert _reasons(_failures("client", {"const": "project"})) == ["invalid_value"]
    assert _failures("project", {"const": "project"}) == []


def test_length_keywords_count_code_points():
    assert _reasons(_failures("\u00e9", {"minLength": 2})) == ["invalid_value"]  # one code point, two bytes
    assert _failures("e\u0301", {"minLength": 2}) == []  # two code points, three UTF-8 bytes
    assert _reasons(_failures("ab", {"maxLength": 1})) == ["invalid_value"]
    assert _failures("ab", {"maxLength": 2}) == []


def test_pattern_never_matches_before_a_trailing_newline():
    assert _reasons(_failures("acme\n", {"pattern": "^[a-z]+$"})) == ["invalid_value"]
    assert _failures("acme", {"pattern": "^[a-z]+$"}) == []
    assert _failures("acme", {"pattern": "c"}) == []  # a search, not a full match


def test_numeric_keywords():
    assert _reasons(_failures(0, {"minimum": 1})) == ["invalid_value"]
    assert _failures(1, {"minimum": 1}) == []
    assert _reasons(_failures(101, {"maximum": 100})) == ["invalid_value"]
    assert _failures(100, {"maximum": 100}) == []


def test_array_keywords():
    assert _reasons(_failures([], {"minItems": 1})) == ["invalid_value"]
    assert _failures(["a"], {"minItems": 1}) == []
    assert _reasons(_failures(["a", "b"], {"maxItems": 1})) == ["invalid_value"]
    assert _reasons(_failures([1], {"type": "array", "items": {"type": "string"}})) == ["invalid_value"]
    assert _failures(["a"], {"type": "array", "items": {"type": "string"}}) == []


def test_required_reports_a_missing_field_at_the_property():
    schema = {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
    assert _pairs(_failures({}, schema)) == [("query", "missing_field")]


def test_additional_properties_reports_the_unexpected_field():
    schema = {"type": "object", "properties": {"query": {"type": "string"}}, "additionalProperties": False}
    assert _pairs(_failures({"query": "a", "extra": 1}, schema)) == [("extra", "unexpected_field")]


def test_nested_paths_are_dotted_and_indexed():
    nested = {"type": "object",
              "properties": {"properties": {"type": "object",
                                            "properties": {"status": {"type": "string"}},
                                            "additionalProperties": False}},
              "additionalProperties": False}
    assert _pairs(_failures({"properties": {"status": 5}}, nested)) == [("properties.status", "invalid_value")]
    sections = {"type": "object",
                "properties": {"sections": {"type": "array", "items": {
                    "type": "object", "properties": {"body": {"type": "string"}},
                    "required": ["body"], "additionalProperties": False}}},
                "additionalProperties": False}
    assert _pairs(_failures({"sections": [{"body": 1}]}, sections)) == [("sections[0].body", "invalid_value")]
    assert _pairs(_failures({"sections": [{}]}, sections)) == [("sections[0].body", "missing_field")]


def test_failures_follow_property_order_not_instance_order():
    schema = {"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
              "required": ["a", "b"], "additionalProperties": False}
    assert _pairs(_failures({"b": 1, "a": 2}, schema)) == [("a", "invalid_value"), ("b", "invalid_value")]
    assert _pairs(_failures({}, schema)) == [("a", "missing_field"), ("b", "missing_field")]
    assert _pairs(_failures({"b": 1, "zzz": True}, schema)) == [
        ("a", "missing_field"), ("b", "invalid_value"), ("zzz", "unexpected_field")]
