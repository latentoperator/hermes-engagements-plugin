"""The post-apply structural check must prove untouched bytes are identical.

TOOLS.md "Section editing rules" requires the re-parsed result's bytes outside
the edited spans to be identical to the input (the edited spans include the
plugin's own ``updated`` and Change log edits).  These tests inject helpers that
corrupt bytes outside the span one operation is authorized to change and
require ``invalid_op`` / ``structure_changed``; the preservation cases keep the
legitimate splices working across LF and CRLF, a BOM, a missing final EOL, a
missing section and Change log, and sequential overlapping edits.
"""
from datetime import UTC, datetime

import pytest
from engagements import records
from engagements.errors import EngagementsError
from engagements.records import _authorized_span, _ByteGuard, apply_ops, parse

EOL = b"\n"
CLOCK = datetime(2026, 11, 7, tzinfo=UTC)


def run(data, ops, kind="client", **kwargs):
    return apply_ops(data, kind, ops, clock=lambda: CLOCK, profile="studio-dev",
                     source="notes", client_exists=lambda _: True, **kwargs)


# Frontmatter comment, level-1 preamble content and a second section body are
# all bytes no operation below is authorized to touch.
BASE = b"""---
engagements: client
id: acme-bakery
# owner: studio
name: Acme Bakery
status: active
---
# Acme Bakery
orphan level-1 content
## Notes
old
## History
kept body
## Change log
previous
"""


def structure_changed(exc):
    return (exc.value.code, exc.value.details.get("reason")) == ("invalid_op", "structure_changed")


# --- Fault injection: helpers that corrupt unauthorized bytes -----------------

@pytest.mark.parametrize(("before", "after"), [
    (b"# owner: studio", b"# owner: studip"),
    (b"orphan level-1 content", b"orphan level-1 contenT"),
    (b"kept body", b"kept b0dy"),
])
def test_status_splice_cannot_corrupt_untouched_regions(monkeypatch, before, after):
    original = records._set_property

    def corrupt(data, key, value):
        return original(data, key, value).replace(before, after, 1)

    monkeypatch.setattr(records, "_set_property", corrupt)
    with pytest.raises(EngagementsError) as exc:
        run(BASE, [{"op": "set_property", "key": "status", "value": "paused"}])
    assert structure_changed(exc)


def test_frontmatter_comment_change_is_refused(monkeypatch):
    original = records._set_property

    def corrupt(data, key, value):
        result = original(data, key, value)
        # Same length, so offset-based span comparisons cannot notice.
        return result.replace(b"# owner: studio", b"# owner: studip", 1)

    monkeypatch.setattr(records, "_set_property", corrupt)
    with pytest.raises(EngagementsError) as exc:
        run(BASE, [{"op": "set_property", "key": "status", "value": "paused"}])
    assert structure_changed(exc)


def test_level_one_content_change_is_refused(monkeypatch):
    original = records._section

    def corrupt(data, title, action, content="", match=None, index=0):
        result, found, created = original(data, title, action, content, match, index)
        if title.strip().casefold() == "notes":
            result = result.replace(b"orphan level-1 content", b"orphan level-1 contenT", 1)
        return result, found, created

    monkeypatch.setattr(records, "_section", corrupt)
    with pytest.raises(EngagementsError) as exc:
        run(BASE, [{"op": "append_to_section", "section": "Notes", "text": "new"}])
    assert structure_changed(exc)


def test_untargeted_section_body_change_is_refused(monkeypatch):
    original = records._section

    def corrupt(data, title, action, content="", match=None, index=0):
        result, found, created = original(data, title, action, content, match, index)
        if title.strip().casefold() == "notes":
            result = result.replace(b"kept body", b"kept b0dy", 1)
        return result, found, created

    monkeypatch.setattr(records, "_section", corrupt)
    with pytest.raises(EngagementsError) as exc:
        run(BASE, [{"op": "append_to_section", "section": "Notes", "text": "new"}])
    assert structure_changed(exc)


def test_change_log_append_helper_cannot_reach_earlier_log_bytes(monkeypatch):
    original = records._section

    def corrupt(data, title, action, content="", match=None, index=0):
        result, found, created = original(data, title, action, content, match, index)
        if title.strip().casefold() == "change log":
            result = result.replace(b"previous", b"previoUs", 1)
        return result, found, created

    monkeypatch.setattr(records, "_section", corrupt)
    with pytest.raises(EngagementsError) as exc:
        run(BASE, [{"op": "set_property", "key": "status", "value": "paused"}])
    assert structure_changed(exc)


def test_line_edit_does_not_exempt_the_rest_of_its_section(monkeypatch):
    data = BASE.replace(b"## Notes\nold\n", b"## Notes\nold\nsecond\n")
    original = records._section

    def corrupt(data, title, action, content="", match=None, index=0):
        result, found, created = original(data, title, action, content, match, index)
        if title.strip().casefold() == "notes":
            # A sibling body line, not the matched one.
            result = result.replace(b"old\nchanged\n", b"Old\nchanged\n", 1)
        return result, found, created

    monkeypatch.setattr(records, "_section", corrupt)
    with pytest.raises(EngagementsError) as exc:
        run(data, [{"op": "replace_line", "section": "Notes", "match": "second", "text": "changed"}])
    assert structure_changed(exc)


# --- Unit-level span model and guard -----------------------------------------

def test_authorized_spans_are_the_exact_edit_targets():
    record = parse(BASE)
    status = BASE.index(b"status: active\n")
    assert _authorized_span(record, BASE, {"op": "set_property", "key": "status", "value": "paused"}) == (
        status, status + len(b"status: active\n"))
    notes_line = BASE.index(b"old\n")
    assert _authorized_span(record, BASE, {"op": "replace_line", "section": "Notes",
                                           "match": "old", "text": "new"}) == (
        notes_line, notes_line + len(b"old\n"))
    # An append is a zero-width insertion after the last non-blank body line;
    # it does not own the whole section.
    assert _authorized_span(record, BASE, {"op": "append_to_section", "section": "Notes",
                                           "text": "new"}) == (notes_line + len(b"old\n"),) * 2
    # replace_section owns exactly its span, from heading to the next heading.
    history = BASE.index(b"## History")
    assert _authorized_span(record, BASE, {"op": "replace_section", "section": "History",
                                           "body": "new"}) == (history, BASE.index(b"## Change log"))


def test_guard_accepts_a_legitimate_splice():
    start, end = BASE.index(b"status: active\n"), BASE.index(b"status: active\n") + len(b"status: active\n")
    after = BASE[:start] + b"status: paused\n" + BASE[end:]
    guard = _ByteGuard(BASE)
    guard.step(BASE, after, start, end)
    guard.verify(after)


def test_guard_rejects_a_change_outside_the_authorized_interval():
    start, end = BASE.index(b"status: active\n"), BASE.index(b"status: active\n") + len(b"status: active\n")
    after = BASE[:start] + b"status: paused\n" + BASE[end:]
    tampered = after.replace(b"# owner: studio", b"# owner: studip", 1)
    guard = _ByteGuard(BASE)
    with pytest.raises(EngagementsError) as exc:
        guard.step(BASE, tampered, start, end)
    assert structure_changed(exc)


def test_guard_verify_detects_tampering_after_a_validated_step():
    start, end = BASE.index(b"status: active\n"), BASE.index(b"status: active\n") + len(b"status: active\n")
    after = BASE[:start] + b"status: paused\n" + BASE[end:]
    guard = _ByteGuard(BASE)
    guard.step(BASE, after, start, end)
    tampered = after.replace(b"orphan level-1 content", b"orphan level-1 contenT", 1)
    with pytest.raises(EngagementsError) as exc:
        guard.verify(tampered)
    assert structure_changed(exc)


def test_comments_and_preamble_survive_a_property_edit():
    result = run(BASE, [{"op": "set_property", "key": "status", "value": "paused"}])
    assert b"# owner: studio\n" in result
    assert b"# Acme Bakery\norphan level-1 content\n" in result
    assert b"## History\nkept body\n" in result
    assert parse(result).properties["status"] == "paused"


@pytest.mark.parametrize("eol", [b"\n", b"\r\n"])
@pytest.mark.parametrize("bom", [b"", b"\xef\xbb\xbf"])
@pytest.mark.parametrize("final", [True, False])
def test_untouched_bytes_survive_every_line_ending(tmp_path, eol, bom, final):
    data = bom + BASE.replace(b"\n", eol)
    if not final:
        data = data[:-len(eol)]
    result = run(data, [{"op": "append_to_section", "section": "Notes", "text": "new"}])
    expected_comment = b"# owner: studio" + eol
    expected_preamble = b"# Acme Bakery" + eol + b"orphan level-1 content" + eol
    expected_history = b"## History" + eol + b"kept body" + eol
    assert result.startswith(bom)
    assert expected_comment in result
    assert expected_preamble in result
    assert expected_history in result
    assert result.endswith(eol) is final
    assert parse(result).final_newline is final


def test_missing_section_creation_preserves_untouched_bytes():
    result = run(BASE, [{"op": "append_to_section", "section": "Contacts", "text": "Ada"}])
    assert b"# owner: studio\n" in result
    assert b"orphan level-1 content\n" in result
    assert b"## History\nkept body\n" in result
    assert b"## Contacts\nAda\n\n## Change log\n" in result


def test_missing_change_log_creation_preserves_untouched_bytes():
    data = BASE.replace(b"## Change log\nprevious\n", b"")
    result = run(data, [{"op": "set_property", "key": "status", "value": "paused"}])
    assert b"# owner: studio\n" in result
    assert b"orphan level-1 content\n" in result
    assert b"## History\nkept body\n" in result
    assert b"## Change log\n- 2026-11-07 studio-dev: set status (source: notes)\n" in result


def test_sequential_overlapping_edits_preserve_untouched_bytes():
    result = run(BASE, [
        {"op": "append_to_section", "section": "Notes", "text": "alpha"},
        {"op": "append_to_section", "section": "Notes", "text": "beta"},
        {"op": "replace_line", "section": "Notes", "match": "beta", "text": "gamma"},
        {"op": "set_property", "key": "status", "value": "paused"},
        {"op": "set_property", "key": "status", "value": "former"},
    ])
    assert b"## Notes\nold\nalpha\ngamma\n" in result
    assert b"# owner: studio\n" in result
    assert b"orphan level-1 content\n" in result
    assert b"## History\nkept body\n" in result
    assert parse(result).properties["status"] == "former"


def test_replacing_a_section_leaves_other_sections_and_preamble_identical():
    result = run(BASE, [{"op": "replace_section", "section": "Notes", "body": "new"}])
    assert b"# owner: studio\n" in result
    assert b"orphan level-1 content\n" in result
    assert b"## History\nkept body\n" in result
    assert b"## Notes\nnew\n" in result
