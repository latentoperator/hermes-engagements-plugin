"""Contract tests for validate_ops against TOOLS.md "engagements_update".

These cover the handler/test contract: the six per-op schemas (stage 1),
reserved-name precedence, single-line and length rules, and the stage 4
kind and value rules. Application-only rules (match_count, section_missing,
structure_changed, too_large) belong to the apply_ops suite.

TOOLS.md is normative here; comments cite the rule each case enforces.
"""

import copy

import pytest
from engagements.errors import EngagementsError
from engagements.records import validate_ops

CLIENT = "client"
PROJECT = "project"


def known_client(value):
    return value == "acme-bakery"


def validate(kind, ops, *, client_exists=known_client):
    return validate_ops(kind, ops, client_exists=client_exists)


def failure(kind, ops, *, client_exists=known_client):
    with pytest.raises(EngagementsError) as caught:
        validate(kind, ops, client_exists=client_exists)
    return caught.value


def op_failure(op, kind=CLIENT, *, client_exists=known_client):
    return failure(kind, [op], client_exists=client_exists)


def assert_invalid(error, index, field, reason):
    assert error.code == "invalid_op"
    assert error.details == {"op_index": index, "field": field, "reason": reason}


# Every schema in TOOLS.md "Per-op validation schemas" has a success case,
# including both set_property shapes and every allowed key per kind.

VALID_OPS = [
    (CLIENT, {"op": "set_property", "key": "name", "value": "Acme Bakery"}),
    (CLIENT, {"op": "set_property", "key": "status", "value": "active"}),
    (CLIENT, {"op": "set_property", "key": "status", "value": "paused"}),
    (CLIENT, {"op": "set_property", "key": "status", "value": "former"}),
    (PROJECT, {"op": "set_property", "key": "status", "value": "active"}),
    (PROJECT, {"op": "set_property", "key": "status", "value": "historical"}),
    (PROJECT, {"op": "set_property", "key": "client", "value": "acme-bakery"}),
    (CLIENT, {"op": "set_property", "key": "aliases", "values": ["Acme", "ACME Bakery"]}),
    (CLIENT, {"op": "set_property", "key": "aliases", "values": []}),
    (PROJECT, {"op": "set_property", "key": "locations",
               "values": ["~/clients/acme", "https://git.example/acme"]}),
    (CLIENT, {"op": "replace_section", "section": "Summary", "body": ""}),
    (PROJECT, {"op": "replace_section", "section": "Purpose", "body": "Rebuild the site."}),
    (CLIENT, {"op": "append_to_section", "section": "Summary", "text": "- weekly status"}),
    (PROJECT, {"op": "append_to_section", "section": "Notes", "text": "Kickoff 2026-11-07."}),
    (CLIENT, {"op": "replace_line", "section": "Contacts", "match": "jane@", "text": "- Jane | owner"}),
    (PROJECT, {"op": "remove_line", "section": "Notes", "match": "obsolete"}),
]


@pytest.mark.parametrize(("kind", "op"), VALID_OPS)
def test_each_per_op_schema_accepts_a_valid_op(kind, op):
    assert validate(kind, [op]) is None


def test_ops_apply_as_a_mixed_batch():
    ops = [
        {"op": "set_property", "key": "name", "value": "Acme Bakery"},
        {"op": "set_property", "key": "aliases", "values": ["Acme"]},
        {"op": "replace_section", "section": "Summary", "body": ""},
        {"op": "append_to_section", "section": "Summary", "text": "- weekly status"},
        {"op": "replace_line", "section": "Contacts", "match": "jane@", "text": "- Jane | owner"},
        {"op": "remove_line", "section": "Notes", "match": "obsolete"},
    ]
    assert validate(CLIENT, ops) is None


# Stage 1: schema, per-op shape, single-line and length rules.


@pytest.mark.parametrize(("op", "field", "reason"), [
    ({"key": "name", "value": "Acme"}, "op", "missing_field"),
    ({"op": None}, "op", "invalid_value"),
    ({"op": 7}, "op", "invalid_value"),
    ({"op": "delete_everything"}, "op", "invalid_value"),
    ({"op": "set_property", "value": "Acme"}, "key", "missing_field"),
    ({"op": "set_property", "key": "slug", "value": "acme"}, "key", "invalid_value"),
    ({"op": "set_property", "key": 3, "value": "acme"}, "key", "invalid_value"),
    ({"op": "set_property", "key": "name"}, "value", "missing_field"),
    ({"op": "set_property", "key": "name", "value": None}, "value", "invalid_value"),
    ({"op": "set_property", "key": "name", "value": 12}, "value", "invalid_value"),
    ({"op": "set_property", "key": "name", "value": ""}, "value", "invalid_value"),
    ({"op": "set_property", "key": "name", "value": "   "}, "value", "invalid_value"),
    ({"op": "set_property", "key": "name", "value": "x" * 201}, "value", "invalid_value"),
    ({"op": "set_property", "key": "aliases"}, "values", "missing_field"),
    ({"op": "set_property", "key": "aliases", "values": None}, "values", "invalid_value"),
    ({"op": "set_property", "key": "aliases", "values": "Acme"}, "values", "invalid_value"),
    ({"op": "set_property", "key": "aliases", "values": ["x"] * 51}, "values", "invalid_value"),
    ({"op": "set_property", "key": "aliases", "values": [1]}, "values", "invalid_value"),
    ({"op": "set_property", "key": "aliases", "values": [""]}, "values", "invalid_value"),
    ({"op": "set_property", "key": "aliases", "values": ["x" * 1025]}, "values", "invalid_value"),
    ({"op": "replace_section", "body": "x"}, "section", "missing_field"),
    ({"op": "replace_section", "section": "Summary"}, "body", "missing_field"),
    ({"op": "replace_section", "section": "Summary", "body": None}, "body", "invalid_value"),
    ({"op": "replace_section", "section": 4, "body": "x"}, "section", "invalid_value"),
    ({"op": "replace_section", "section": "x" * 201, "body": ""}, "section", "invalid_value"),
    ({"op": "replace_section", "section": "Summary", "body": "x" * 20001}, "body", "invalid_value"),
    ({"op": "append_to_section", "section": "Summary"}, "text", "missing_field"),
    ({"op": "append_to_section", "section": "Summary", "text": 1}, "text", "invalid_value"),
    ({"op": "append_to_section", "section": "Summary", "text": "x" * 20001}, "text", "invalid_value"),
    ({"op": "replace_line", "section": "Summary", "match": "a"}, "text", "missing_field"),
    ({"op": "replace_line", "section": "Summary", "text": "b"}, "match", "missing_field"),
    ({"op": "replace_line", "match": "a", "text": "b"}, "section", "missing_field"),
    ({"op": "replace_line", "section": "Summary", "match": None, "text": "b"}, "match", "invalid_value"),
    ({"op": "replace_line", "section": "Summary", "match": "", "text": "b"}, "match", "invalid_value"),
    ({"op": "replace_line", "section": "Summary", "match": "x" * 1001, "text": "b"}, "match", "invalid_value"),
    ({"op": "replace_line", "section": "Summary", "match": "a", "text": ""}, "text", "invalid_value"),
    ({"op": "replace_line", "section": "Summary", "match": "a", "text": "x" * 1001},
     "text", "invalid_value"),
    ({"op": "remove_line", "section": "Summary"}, "match", "missing_field"),
    ({"op": "remove_line", "match": "a"}, "section", "missing_field"),
    ({"op": "remove_line", "section": 5, "match": "a"}, "section", "invalid_value"),
    ({"op": "remove_line", "section": "Summary", "match": "x" * 1001}, "match", "invalid_value"),
])
def test_schema_violations_are_invalid_op(op, field, reason):
    error = op_failure(op)
    assert_invalid(error, 0, field, reason)


@pytest.mark.parametrize(("op", "field"), [
    ({"op": "set_property", "key": "name", "value": "x", "body": "y"}, "body"),
    ({"op": "set_property", "key": "aliases", "values": [], "match": "m"}, "match"),
    ({"op": "replace_section", "section": "Summary", "body": "", "match": "m"}, "match"),
    ({"op": "append_to_section", "section": "Summary", "text": "x", "body": "b"}, "body"),
    ({"op": "replace_line", "section": "Summary", "match": "m", "text": "t", "body": "b"}, "body"),
    ({"op": "remove_line", "section": "Summary", "match": "m", "text": "t"}, "text"),
])
def test_additional_properties_are_unexpected_fields(op, field):
    error = op_failure(op)
    assert_invalid(error, 0, field, "unexpected_field")


@pytest.mark.parametrize("kind,op", [
    (PROJECT, {"op": "set_property", "key": "client", "value": "acme-bakery"}),
    (PROJECT, {"op": "set_property", "key": "aliases", "values": ["Acme"]}),
    (PROJECT, {"op": "remove_line", "section": "Notes", "match": "m"}),
])
def test_extra_field_on_the_wrong_schema_is_unexpected(kind, op):
    # "An op must validate against exactly the schema selected by its op
    # (and, for set_property, by whether key is a scalar or list key)."
    extra = {"value": "x"} if "value" not in op and op["op"] == "set_property" else {"text": "t"}
    error = op_failure({**op, **extra}, kind)
    assert error.code == "invalid_op"
    assert error.details["reason"] == "unexpected_field"


@pytest.mark.parametrize("text", [
    "\n", "\r", "a\nb", "\x00", "\x1f", "\x7f", "a\x85b", "a\u2028b", "a\u2029b",
    "a\ud800", "a\ufffe", "a\uffff", "   ",
])
def test_single_line_rule_rejects_line_breaks_controls_and_blanks(text):
    # "single-line string: nonblank after trimming, no \r, \n, or other C0/C1
    # control characters, no U+2028 or U+2029, no surrogate code points, and
    # no U+FFFE or U+FFFF."
    cases = [
        (CLIENT, {"op": "set_property", "key": "name", "value": text}, "value"),
        (CLIENT, {"op": "set_property", "key": "aliases", "values": [text]}, "values"),
        (PROJECT, {"op": "set_property", "key": "locations", "values": [text]}, "values"),
        (CLIENT, {"op": "remove_line", "section": text, "match": "m"}, "section"),
        (CLIENT, {"op": "replace_line", "section": "Summary", "match": "m", "text": text}, "text"),
    ]
    for kind, op, field in cases:
        assert_invalid(op_failure(op, kind), 0, field, "invalid_value")


@pytest.mark.parametrize(("kind", "op"), [
    (CLIENT, {"op": "set_property", "key": "name", "value": "x" * 200}),
    (CLIENT, {"op": "set_property", "key": "aliases", "values": ["x" * 1024]}),
    (CLIENT, {"op": "set_property", "key": "aliases", "values": ["x"] * 50}),
    (CLIENT, {"op": "replace_section", "section": "x" * 200, "body": "x" * 20000}),
    (CLIENT, {"op": "append_to_section", "section": "Summary", "text": "x" * 20000}),
    (CLIENT, {"op": "replace_line", "section": "Summary", "match": "m" * 1000, "text": "x" * 1000}),
    (CLIENT, {"op": "replace_line", "section": "Summary", "match": "a\nb", "text": "x"}),
])
def test_length_limits_accept_their_documented_maximum(kind, op):
    assert validate(kind, [op]) is None


def test_match_is_not_a_single_line_field():
    # "`source` and `section` are single-line. `replace_line.text` is
    # single-line." `match` has no single-line rule, only 1..1000 characters.
    assert validate(CLIENT, [{"op": "remove_line", "section": "S", "match": "a\rb"}]) is None


@pytest.mark.parametrize(("ops", "reason"), [
    ([], "invalid_value"),
    ("not a list", "invalid_value"),
    (None, "invalid_value"),
    (({"op": "set_property", "key": "name", "value": "x"},), "invalid_value"),
    ([{"op": "set_property", "key": "name", "value": "x"}] * 51, "invalid_value"),
])
def test_ops_array_is_bounded_and_typed(ops, reason):
    # minItems 1, maxItems 50. A container failure names the array, not an op.
    error = failure(CLIENT, ops)
    assert error.code == "invalid_op"
    assert error.details == {"field": "ops", "reason": reason}


def test_fifty_ops_are_allowed():
    ops = [{"op": "set_property", "key": "name", "value": "Acme Bakery"}] * 50
    assert validate(CLIENT, ops) is None


# Reserved names win over generic schema rejection (TOOLS.md error precedence).


@pytest.mark.parametrize("op", [
    {"op": "set_property", "key": "updated", "value": "2026-11-07"},
    {"op": "set_property", "key": "updated"},
    {"op": "remove_line", "section": "Summary", "match": "x", "key": "updated"},
    {"op": "remove_line", "section": "Summary", "key": "updated"},
    {"op": "bogus", "key": "updated"},
])
def test_updated_key_is_plugin_owned_before_shape_errors(op):
    assert_invalid(op_failure(op), 0, "key", "plugin_owned")


@pytest.mark.parametrize("section", ["Change log", "change LOG", "  Change   Log  ",
                                     "CHANGE\tLOG", "change \u00a0 log"])
def test_change_log_section_is_plugin_owned(section):
    # T9: any section normalizing to "change log" is plugin_owned.
    ops = [
        {"op": "replace_section", "section": section, "body": "x"},
        {"op": "append_to_section", "section": section, "text": "x"},
        {"op": "replace_line", "section": section, "match": "m", "text": "t"},
        {"op": "remove_line", "section": section, "match": "m"},
        {"op": "set_property", "key": "name", "value": "x", "section": section},
    ]
    for op in ops:
        assert_invalid(op_failure(op), 0, "section", "plugin_owned")


def test_change_log_section_is_plugin_owned_before_missing_fields():
    assert_invalid(op_failure({"op": "remove_line", "section": "Change Log"}), 0,
                   "section", "plugin_owned")


@pytest.mark.parametrize("section", ["Change-log", "Changes log", "Change logs"])
def test_near_miss_section_titles_are_not_plugin_owned(section):
    assert validate(CLIENT, [{"op": "remove_line", "section": section, "match": "m"}]) is None


# Stage 4: kind and value rules.


@pytest.mark.parametrize(("kind", "value"), [
    (CLIENT, "historical"),
    (CLIENT, "Active"),
    (CLIENT, "active "),
    (PROJECT, "paused"),
    (PROJECT, "former"),
])
def test_status_must_fit_the_kind(kind, value):
    op = {"op": "set_property", "key": "status", "value": value}
    assert_invalid(op_failure(op, kind), 0, "value", "status_not_allowed_for_kind")


@pytest.mark.parametrize(("kind", "op"), [
    (CLIENT, {"op": "set_property", "key": "client", "value": "acme-bakery"}),
    (CLIENT, {"op": "set_property", "key": "locations", "values": []}),
])
def test_project_only_keys_are_refused_on_clients(kind, op):
    error = op_failure(op, kind)
    assert_invalid(error, 0, "key", "key_not_allowed_for_kind")


def test_project_only_keys_are_refused_before_client_lookup():
    calls = []
    error = op_failure({"op": "set_property", "key": "client", "value": "acme-bakery"}, CLIENT,
                       client_exists=lambda value: calls.append(value) or True)
    assert error.details["reason"] == "key_not_allowed_for_kind"
    assert calls == []


@pytest.mark.parametrize("client", ["riverbend-dental", "Not An Id", "acme-bakery "])
def test_unknown_client_is_not_found(client):
    # Stage 4 yields `not_found` (unknown client); details carry reason only.
    error = op_failure({"op": "set_property", "key": "client", "value": client}, PROJECT,
                       client_exists=lambda value: value == "acme-bakery")
    assert error.code == "not_found"
    assert error.details == {"reason": "no_match"}


def test_client_lookup_receives_the_candidate_id():
    calls = []
    validate(PROJECT, [{"op": "set_property", "key": "client", "value": "acme-bakery"}],
             client_exists=lambda value: calls.append(value) or True)
    assert calls == ["acme-bakery"]


@pytest.mark.parametrize(("kind", "op"), [
    (CLIENT, {"op": "set_property", "key": "name", "value": "Acme Bakery"}),
    (CLIENT, {"op": "set_property", "key": "status", "value": "paused"}),
    (CLIENT, {"op": "set_property", "key": "aliases", "values": ["Acme"]}),
    (PROJECT, {"op": "set_property", "key": "locations", "values": ["~/acme"]}),
    (CLIENT, {"op": "remove_line", "section": "Notes", "match": "m"}),
])
def test_client_lookup_is_not_consulted_for_other_keys(kind, op):
    calls = []
    validate(kind, [op], client_exists=lambda value: calls.append(value) or True)
    assert calls == []


# Inserted body/text: TOOLS.md "Fences and headings".


@pytest.mark.parametrize(("text", "reason"), [
    ("## Heading", "heading_in_text"),
    ("# Heading", "heading_in_text"),
    (" ## Heading", "heading_in_text"),
    ("   ## Heading", "heading_in_text"),
    ("##\tHeading", "heading_in_text"),
    ("##", "heading_in_text"),
    ("\n## Heading\n", "heading_in_text"),
    ("```\n## fenced\n```\n## after", "heading_in_text"),
    ("#### Deep", None),
    ("    ## Indented", None),
    ("```\n## fenced\n```", None),
    ("~~~\n## fenced\n~~~", None),
    ("```", "unbalanced_fence"),
    ("~~~", "unbalanced_fence"),
    ("````\n```", "unbalanced_fence"),
    ("````\n```\n````", None),
    ("~~~~\n~~~\n~~~~", None),
    ("~~~\nx\n```", "unbalanced_fence"),
    ("```\n~~~~\n```", None),
    ("``` a`b", None),
    ("````x", "unbalanced_fence"),
    ("```\n## fenced", "unbalanced_fence"),
])
def test_inserted_text_fences_and_headings(text, reason):
    op = {"op": "append_to_section", "section": "Summary", "text": text}
    if reason is None:
        assert validate(CLIENT, [op]) is None
    else:
        assert_invalid(op_failure(op), 0, "text", reason)


@pytest.mark.parametrize(("body", "reason"), [
    ("## Heading", "heading_in_text"),
    ("# Heading", "heading_in_text"),
    ("```\n## fenced\n```\n## after", "heading_in_text"),
    ("#### Deep", None),
    ("", None),
    ("   ", None),
    ("\n", None),
    ("```", "unbalanced_fence"),
    ("```\n## fenced\n```", None),
])
def test_replace_section_body_follows_the_same_rules(body, reason):
    op = {"op": "replace_section", "section": "Summary", "body": body}
    if reason is None:
        assert validate(CLIENT, [op]) is None
    else:
        assert_invalid(op_failure(op), 0, "body", reason)


@pytest.mark.parametrize("text", ["", "   ", "\n", "\t\n ", "\r\n  \r\n"])
def test_append_text_must_be_non_empty_after_stripping(text):
    assert_invalid(op_failure({"op": "append_to_section", "section": "Summary", "text": text}),
                   0, "text", "empty_text")


def test_replace_line_text_must_be_non_empty():
    op = {"op": "replace_line", "section": "Summary", "match": "m", "text": "  "}
    assert_invalid(op_failure(op), 0, "text", "invalid_value")


# Error precedence and index reporting.


def test_shape_errors_across_all_ops_precede_value_errors():
    # Stage 1 runs for the whole request before stage 4.
    ops = [
        {"op": "set_property", "key": "status", "value": "historical"},
        {"op": "remove_line", "section": "Contacts"},
    ]
    assert_invalid(failure(CLIENT, ops), 1, "match", "missing_field")


def test_value_errors_report_the_lowest_op_index():
    ops = [
        {"op": "set_property", "key": "status", "value": "paused"},
        {"op": "set_property", "key": "client", "value": "riverbend-dental"},
    ]
    assert_invalid(failure(PROJECT, ops, client_exists=known_client), 0, "value",
                   "status_not_allowed_for_kind")


def test_schema_field_order_follows_the_selected_schema():
    # replace_section properties are op, section, body.
    op = {"op": "replace_section", "section": "", "body": 3}
    assert_invalid(op_failure(op), 0, "section", "invalid_value")


def test_missing_required_field_precedes_unexpected_field():
    # The selected set_property_scalar schema requires `value`; `values` is
    # not allowed. The missing required field is reported first.
    op = {"op": "set_property", "key": "name", "values": ["Acme"]}
    assert_invalid(op_failure(op), 0, "value", "missing_field")


def test_invalid_value_precedes_unexpected_field():
    op = {"op": "set_property", "key": "name", "value": "", "body": "x"}
    assert_invalid(op_failure(op), 0, "value", "invalid_value")


def test_validation_does_not_mutate_the_request():
    ops = [
        {"op": "set_property", "key": "aliases", "values": ["ACME!", "acme", "Bakery"]},
        {"op": "set_property", "key": "name", "value": "Acme Bakery"},
    ]
    snapshot = copy.deepcopy(ops)
    validate(CLIENT, ops)
    assert ops == snapshot


@pytest.mark.parametrize("kind", ["Client", "clients", "project ", "", None, 1])
def test_kind_must_be_resolved_before_validation(kind):
    # kind comes from the resolved record; anything else is a caller bug, the
    # same class as an unknown library defect. It is not user input.
    with pytest.raises(ValueError):
        validate(kind, [{"op": "set_property", "key": "name", "value": "Acme"}])
