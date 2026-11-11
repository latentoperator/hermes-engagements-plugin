"""TOOLS.md "Error precedence": when two errors apply, the earlier stage wins.

Stage 7 beats stage 8 through a read-only record directory (a stage-8 commit
failure) plus an op that fails in memory first; that construction skips itself
for root, whose permission checks never fire.
"""

import os
from pathlib import Path

import pytest
from tools_support import call, copy_library, make_session, rev_of

ACME = "Acme Bakery/Client.md"
JANE = "jane@acme-bakery.example"
SAM = "- Sam Ortiz | manager, Elm St | sam@acme-bakery.example"
STALE = "0" * 16


def edit(path, old, new):
    text = path.read_text(encoding="utf-8")
    assert old in text, old
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def read_base(session, identifier="acme-bakery"):
    result = call("engagements_context", {"id": identifier}, session)
    assert result["ok"], result
    return result["data"]["rev"]


def update(session, base_rev, ops, *, identifier="acme-bakery", source="test 2026-11-07"):
    return call("engagements_update", {"id": identifier, "base_rev": base_rev, "source": source,
                                       "ops": ops}, session)


# --- stage boundaries --------------------------------------------------------


def test_invalid_arguments_beat_a_missing_library(tmp_path):
    session, _ = make_session(tmp_path / "absent")
    result = call("engagements_find", {}, session)
    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_op"
    assert result["error"]["message"] == "Invalid argument."
    assert result["error"]["details"] == {"field": "query", "reason": "missing_field"}


@pytest.mark.parametrize("tool, args, field", (
    pytest.param("engagements_context", {"id": "Acme\nBakery"}, "id", id="context-id"),
    pytest.param("engagements_context", {"id": "Acme\u2028Bakery"}, "id", id="context-id-line-separator"),
    pytest.param("engagements_bind", {"client": "Acme\nBakery"}, "client", id="bind-client"),
    pytest.param("engagements_bind", {"project": "Acme\nBakery"}, "project", id="bind-project"),
))
def test_a_reference_that_is_not_one_line_is_rejected_before_the_library_opens(tmp_path, tool, args, field):
    session, state = make_session(tmp_path / "absent")
    result = call(tool, args, session)
    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_op"
    assert result["error"]["details"] == {"field": field, "reason": "invalid_value"}
    assert state == {}


def test_a_rejected_reference_does_not_create_the_default_library(tmp_path):
    root = tmp_path / "default-library"
    session, _ = make_session(root, create_default=True)
    result = call("engagements_context", {"id": "Acme\nBakery"}, session)
    assert result["error"]["code"] == "invalid_op"
    assert not root.exists()


def test_a_reference_with_a_trailing_newline_is_not_matched_to_a_name(tmp_path):
    root = copy_library(tmp_path)
    session, _ = make_session(root)
    for tool, args, field in (("engagements_context", {"id": "acme-bakery\n"}, "id"),
                              ("engagements_bind", {"client": "acme-bakery\n"}, "client")):
        result = call(tool, args, session)
        assert result["error"]["code"] == "invalid_op"
        assert result["error"]["details"]["field"] == field


def test_library_missing_beats_not_found(tmp_path):
    session, _ = make_session(tmp_path / "absent")
    result = call("engagements_context", {"id": "nope-1"}, session)
    assert result["ok"] is False
    assert result["error"]["code"] == "library_missing"


def test_not_found_beats_a_value_error(tmp_path):
    root = copy_library(tmp_path)
    session, _ = make_session(root)
    result = update(session, STALE, [{"op": "set_property", "key": "status", "value": "bogus"}],
                    identifier="nope-1")
    assert result["error"]["code"] == "not_found"
    assert result["error"]["details"]["reason"] == "no_match"


def test_ambiguous_beats_a_value_error(tmp_path):
    root = copy_library(tmp_path)
    duplicate = root / "Duplicate Bakery" / "Client.md"
    duplicate.parent.mkdir()
    duplicate.write_text((root / ACME).read_text(encoding="utf-8"), encoding="utf-8")
    session, _ = make_session(root)
    result = update(session, STALE, [{"op": "set_property", "key": "status", "value": "bogus"}])
    assert result["error"]["code"] == "ambiguous"
    paths = {candidate["path"] for candidate in result["error"]["details"]["candidates"]}
    assert paths == {ACME, "Duplicate Bakery/Client.md"}


def test_invalid_value_beats_a_stale_rev(tmp_path):
    root = copy_library(tmp_path)
    session, _ = make_session(root)
    base = read_base(session)
    edit(root / ACME, JANE, "jane@acme.example")
    result = update(session, base, [{"op": "set_property", "key": "status", "value": "bogus"}])
    assert result["error"]["code"] == "invalid_op"
    assert result["error"]["details"] == {"op_index": 0, "field": "value",
                                          "reason": "status_not_allowed_for_kind"}


def test_malformed_beats_a_stale_rev(tmp_path):
    root = copy_library(tmp_path)
    path = root / ACME
    stale = rev_of(path)
    path.write_text(path.read_text(encoding="utf-8").replace("name: Acme Bakery\n", "", 1),
                    encoding="utf-8")
    session, _ = make_session(root)
    result = update(session, stale, [{"op": "set_property", "key": "status", "value": "paused"}])
    assert result["error"]["code"] == "malformed"
    assert result["error"]["details"]["path"] == ACME
    assert {"kind": "missing_field"} in result["error"]["details"]["defects"]


def test_conflict_beats_a_match_count_error(tmp_path):
    root = copy_library(tmp_path)
    session, _ = make_session(root)
    base = read_base(session)
    edit(root / ACME, "email preferred, no calls before 10am\n",
         f"email preferred, no calls before 10am\n{SAM}\n")
    result = update(session, base, [{"op": "replace_line", "section": "Contacts",
                                     "match": "no such line", "text": "- Nobody"}])
    assert result["error"]["code"] == "conflict"
    details = result["error"]["details"]
    assert details["reason"] == "footprint_changed"
    assert [footprint["key"] for footprint in details["footprints"]] == ["contacts"]


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0,
                    reason="permission bits do not bind as root")
def test_apply_error_beats_a_commit_error(tmp_path):
    root = copy_library(tmp_path)
    session, _ = make_session(root)
    base = read_base(session)
    directory = root / "Acme Bakery"
    directory.chmod(0o555)
    try:
        blocked = update(session, base, [{"op": "set_property", "key": "status", "value": "paused"}])
        failing = update(session, base, [{"op": "replace_line", "section": "Contacts",
                                          "match": "no such line", "text": "- Nobody"}])
    finally:
        directory.chmod(0o755)
    assert blocked["error"]["code"] == "io_error"
    assert failing["error"]["code"] == "invalid_op"
    assert failing["error"]["details"]["reason"] == "match_count"


# --- the rulings inside stage 1 and stage 3 ----------------------------------


def test_reserved_names_beat_schema_rejection(tmp_path):
    root = copy_library(tmp_path)
    session, _ = make_session(root)
    base = read_base(session)

    changed = update(session, base, [{"op": "set_property", "key": "updated",
                                      "value": "2026-01-01", "junk": 1}])
    assert changed["error"]["code"] == "invalid_op"
    assert changed["error"]["details"] == {"op_index": 0, "field": "key", "reason": "plugin_owned"}

    created = call("engagements_create", {"kind": "client", "name": "Reserved Co",
                                          "properties": {"updated": "2026-01-01", "bogus": 1}}, session)
    assert created["error"]["details"] == {"field": "properties.updated", "reason": "plugin_owned"}

    section = call("engagements_create", {"kind": "client", "name": "Reserved Two",
                                          "sections": [{"section": "Change Log", "body": "x"}]}, session)
    assert section["error"]["details"] == {"field": "sections[0].section", "reason": "plugin_owned"}

    op_section = update(session, base, [{"op": "replace_section", "section": "  change   LOG ", "body": "x"}])
    assert op_section["error"]["details"] == {"op_index": 0, "field": "section", "reason": "plugin_owned"}


def test_no_session_is_checked_after_arguments_and_before_the_library(tmp_path):
    root = copy_library(tmp_path)
    session, _ = make_session(root, session_id=None)
    arguments_first = call("engagements_bind", {"mode": "explicit"}, session)
    assert arguments_first["error"]["code"] == "invalid_op"
    assert arguments_first["error"]["details"] == {"field": "mode", "reason": "explicit_requires_target"}

    no_library, _ = make_session(None, session_id=None)
    binding = call("engagements_bind", {"client": "acme-bakery"}, no_library)
    assert binding["error"]["code"] == "no_session"
    assert binding["error"]["details"] == {}


def test_context_offset_out_of_range_beats_rev_changed(tmp_path):
    root = copy_library(tmp_path)
    session, _ = make_session(root)
    result = call("engagements_context", {"id": "acme-bakery", "offset": 100000, "rev": STALE}, session)
    assert result["error"]["code"] == "invalid_op"
    assert result["error"]["details"] == {"field": "offset", "reason": "offset_out_of_range"}


def test_context_id_and_path_beat_a_missing_library(tmp_path):
    session, _ = make_session(None)
    result = call("engagements_context", {"id": "acme-bakery", "path": ACME}, session)
    assert result["error"]["code"] == "invalid_op"
    assert result["error"]["details"] == {"field": "path", "reason": "id_and_path"}


def test_unexpected_fields_are_checked_after_properties(tmp_path):
    root = copy_library(tmp_path)
    session, _ = make_session(root)
    missing = call("engagements_find", {"bogus": 1}, session)
    assert missing["error"]["details"] == {"field": "query", "reason": "missing_field"}

    wrong_type = call("engagements_find", {"query": 5, "bogus": 1}, session)
    assert wrong_type["error"]["details"] == {"field": "query", "reason": "invalid_value"}

    unexpected = call("engagements_find", {"query": "acme", "bogus": 1}, session)
    assert unexpected["error"]["details"] == {"field": "bogus", "reason": "unexpected_field"}


def test_an_unknown_client_is_stage_four_not_found(tmp_path):
    root = copy_library(tmp_path)
    session, _ = make_session(root)
    base = read_base(session, "acme-website-2026")
    result = update(session, base, [{"op": "set_property", "key": "client",
                                     "value": "no-such-client"}], identifier="acme-website-2026")
    assert result["error"]["code"] == "not_found"
    assert result["error"]["details"]["reason"] == "no_match"
    assert Path(root, ACME).exists()


# --- contention against another stage ----------------------------------------


def hold_record_lock(root, monkeypatch, holder="acme-bakery"):
    """Hold one record's lock and give the handler a short, deterministic timeout.

    ``flock`` is per open file description, so a lock held here contends with the
    handler's own acquisition even inside one process. The writer is wrapped only
    to shorten ``lock_timeout``; the call still goes through ``call_tool``.
    """
    from engagements import writer as writer_module
    from engagements.fsutil import FileLock

    locks = root / ".engagements" / "locks"
    locks.mkdir(exist_ok=True)
    original = writer_module.update

    def short(*args, **kwargs):
        kwargs.setdefault("lock_timeout", 0.05)
        return original(*args, **kwargs)

    monkeypatch.setattr(writer_module, "update", short)
    return FileLock(locks / f"{holder}.lock", timeout=1)


def test_lock_timeout_beats_a_stale_revision(tmp_path, monkeypatch):
    """Stage 5 fires before stage 6: a held lock outranks a conflict.

    The submitted op must target a footprint that really changed, or the old
    revision is safely rebasable and no stage-6 conflict competes at all.
    The no-lock control proves the conflict applies first.
    """
    root = copy_library(tmp_path)
    session, _ = make_session(root)
    base = read_base(session)
    edit(root / ACME, "email preferred, no calls before 10am\n",
         f"email preferred, no calls before 10am\n{SAM}\n")
    stale_bytes = (root / ACME).read_bytes()
    ops = [{"op": "append_to_section", "section": "Contacts", "text": "- Nobody"}]

    control = update(session, base, ops)
    assert control["error"]["code"] == "conflict", control
    assert control["error"]["details"]["reason"] == "footprint_changed"
    assert [footprint["key"] for footprint in control["error"]["details"]["footprints"]] == ["contacts"]

    with hold_record_lock(root, monkeypatch):
        result = update(session, base, ops)

    assert result["error"]["code"] == "lock_timeout"
    assert (root / ACME).read_bytes() == stale_bytes
    assert not (root / ".engagements" / "history").exists()


def test_a_stage_four_value_error_beats_a_held_lock(tmp_path, monkeypatch):
    root = copy_library(tmp_path)
    session, _ = make_session(root)
    base = read_base(session)
    before = (root / ACME).read_bytes()

    with hold_record_lock(root, monkeypatch):
        result = update(session, base, [{"op": "set_property", "key": "status", "value": "bogus"}])

    assert result["error"]["code"] == "invalid_op"
    assert result["error"]["details"] == {"op_index": 0, "field": "value",
                                          "reason": "status_not_allowed_for_kind"}
    assert (root / ACME).read_bytes() == before


def test_a_stage_four_defect_beats_a_held_lock(tmp_path, monkeypatch):
    root = copy_library(tmp_path)
    session, _ = make_session(root)
    base = read_base(session)
    path = root / ACME
    path.write_text(path.read_text(encoding="utf-8").replace("name: Acme Bakery\n", "", 1), encoding="utf-8")
    before = path.read_bytes()

    with hold_record_lock(root, monkeypatch):
        result = update(session, base, [{"op": "set_property", "key": "status", "value": "paused"}])

    assert result["error"]["code"] == "malformed"
    assert {"kind": "missing_field"} in result["error"]["details"]["defects"]
    assert path.read_bytes() == before


# --- stage 1 single-line rules -------------------------------------------------


def test_a_multiline_source_is_stage_one_before_target_resolution(tmp_path):
    """TOOLS.md line 67: single-line checks are stage 1, so they beat not_found."""
    root = copy_library(tmp_path)
    session, _ = make_session(root)
    result = update(session, STALE, [{"op": "set_property", "key": "status", "value": "paused"}],
                    identifier="no-such-id", source="line one\nline two")
    assert result["error"]["code"] == "invalid_op"
    assert result["error"]["details"] == {"field": "source", "reason": "invalid_value"}


def test_a_multiline_source_is_rejected_for_an_existing_record_too(tmp_path):
    root = copy_library(tmp_path)
    session, _ = make_session(root)
    base = read_base(session)
    before = (root / ACME).read_bytes()
    result = update(session, base, [{"op": "set_property", "key": "status", "value": "paused"}],
                    source="line one\nline two")
    assert result["error"]["code"] == "invalid_op"
    assert result["error"]["details"] == {"field": "source", "reason": "invalid_value"}
    assert (root / ACME).read_bytes() == before
