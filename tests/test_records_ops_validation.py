"""Operation validation at the records boundary."""

import pytest
from engagements.errors import EngagementsError
from engagements.records import validate_ops


@pytest.mark.parametrize("kind,op,reason", [
    ("client", {"op": "set_property", "key": "status", "value": "historical"}, "status_not_allowed_for_kind"),
    ("project", {"op": "set_property", "key": "status", "value": "former"}, "status_not_allowed_for_kind"),
    ("client", {"op": "set_property", "key": "locations", "values": []}, "key_not_allowed_for_kind"),
    ("client", {"op": "set_property", "key": "updated", "value": "today"}, "plugin_owned"),
    ("client", {"op": "remove_line", "section": "Change log", "match": "x"}, "plugin_owned"),
    ("client", {"op": "remove_line", "section": "Summary", "match": "x", "body": "x"}, "unexpected_field"),
    ("client", {"op": "append_to_section", "section": "Summary", "text": "## New"}, "heading_in_text"),
    ("client", {"op": "append_to_section", "section": "Summary", "text": "```"}, "unbalanced_fence"),
    ("client", {"op": "append_to_section", "section": "Summary", "text": "  \n"}, "empty_text"),
    ("client", {"op": "set_property", "key": "name", "value": "bad\ud800"}, "invalid_value"),
    ("client", {"op": "set_property", "key": "name", "value": "bad\ufffe"}, "invalid_value"),
])
def test_invalid_op(kind, op, reason):
    with pytest.raises(EngagementsError) as caught:
        validate_ops(kind, [op], client_exists=lambda _: True)
    assert caught.value.code == "invalid_op"
    assert caught.value.details["reason"] == reason


def test_valid_op_shapes():
    ops = [
        {"op": "set_property", "key": "name", "value": "Acme Bakery"},
        {"op": "set_property", "key": "aliases", "values": ["Acme", "ACME"]},
        {"op": "replace_section", "section": "Summary", "body": ""},
        {"op": "append_to_section", "section": "Summary", "text": "```\n## code\n```"},
        {"op": "replace_line", "section": "Summary", "match": "old", "text": "new"},
        {"op": "remove_line", "section": "Summary", "match": "old"},
    ]
    validate_ops("client", ops, client_exists=lambda _: True)
