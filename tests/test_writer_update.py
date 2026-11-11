"""update: same-rev apply, rebase, footprint conflicts, base_unavailable."""
import stat
import sys

import pytest
from engagements import writer
from engagements.errors import EngagementsError
from engagements.fsutil import rev
from engagements.records import parse
from writer_support import (
    ACME,
    SAM,
    WEBSITE,
    do_update,
    fixed_clock,
    make_library,
    op_comm_append,
    op_contacts_replace,
    op_status,
    tree_snapshot,
)


def conflict_of(lib, base_rev, ops, **kw):
    with pytest.raises(EngagementsError) as exc:
        do_update(lib, base_rev, ops, **kw)
    assert exc.value.code == "conflict"
    return exc.value


# ---- same base revision -------------------------------------------------

def test_same_rev_applies_and_reports_the_documented_shape(lib):
    base = lib.base()
    result = do_update(lib, base, [op_status("paused")])
    data = result.data
    assert data["rebased"] is False
    assert data["id"] == "acme-bakery"
    assert data["path"] == "Acme Bakery/Client.md"
    assert data["applied"] == 1
    assert data["change_log_line"] == "- 2026-11-07 studio-dev: set status (source: owner email 2026-11-07)"
    assert set(data) == {"id", "path", "new_rev", "rebased", "applied", "change_log_line"}
    assert result.warnings == []
    assert data["new_rev"] == rev(lib.read())
    assert parse(lib.read()).properties["status"] == "paused"
    assert parse(lib.read()).properties["updated"] == "2026-11-07"


def test_new_rev_is_snapshotted(lib):
    base = lib.base()
    data = do_update(lib, base, [op_status("paused")]).data
    assert writer.load_snapshot(lib.root, data["new_rev"]) == lib.read()
    assert writer.load_snapshot(lib.root, base) is not None


def test_ops_apply_in_order_and_summaries_join(lib):
    base = lib.base()
    data = do_update(lib, base, [op_comm_append(), op_status("paused")]).data
    assert data["applied"] == 2
    assert data["change_log_line"].endswith(
        "studio-dev: append to Communication preferences; set status (source: owner email 2026-11-07)")
    text = lib.read().decode()
    assert text.rstrip("\n").endswith(data["change_log_line"])


def test_a_chained_update_from_new_rev_is_not_rebased(lib):
    first = do_update(lib, lib.base(), [op_status("paused")]).data
    second = do_update(lib, first["new_rev"], [op_status("active")]).data
    assert second["rebased"] is False


def test_crlf_bom_and_missing_final_newline_are_preserved(tmp_path):
    raw = b"\xef\xbb\xbf" + ACME.replace("\n", "\r\n").rstrip("\r\n").encode()
    lib = make_library(tmp_path, raw)
    result = do_update(lib, lib.base(), [op_comm_append("- Quarterly call")])
    out = lib.read()
    assert out.startswith(b"\xef\xbb\xbf") and b"\r\n" in out and b"\n" not in out.replace(b"\r\n", b"")
    assert not out.endswith(b"\n")
    assert result.data["change_log_line"] in out.decode()


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no POSIX permission bits")
def test_file_mode_is_preserved(lib):
    lib.path.chmod(0o640)
    do_update(lib, lib.base(), [op_status("paused")])
    assert stat.S_IMODE(lib.path.stat().st_mode) == 0o640


def test_no_temp_files_remain_after_success(lib):
    do_update(lib, lib.base(), [op_status("paused")])
    assert lib.leftovers() == []


# ---- rebase and footprint conflicts ---------------------------------------

def test_stale_rev_with_unchanged_footprints_rebases_onto_current(lib):
    base = lib.base()
    lib.edit("## Contacts\n", "## Contacts\n" + SAM + "\n")  # editor touches Contacts only
    external = lib.read()
    result = do_update(lib, base, [op_comm_append()])
    assert result.data["rebased"] is True
    out = lib.read().decode()
    assert SAM in out and "- Monthly review call" in out
    assert result.data["new_rev"] == rev(lib.read())
    assert external.decode().split("## Change log")[0].count(SAM) == 1


def test_stale_rev_with_a_changed_footprint_conflicts_and_writes_nothing(lib):
    base = lib.base()
    lib.edit("## Contacts\n- Jane Rivera", "## Contacts\n" + SAM + "\n- Jane Rivera")
    before = tree_snapshot(lib.root)
    error = conflict_of(lib, base, [op_contacts_replace()])
    details = error.details
    assert details["reason"] == "footprint_changed"
    assert details["current_rev"] == rev(lib.read())
    assert len(details["footprints"]) == 1
    footprint = details["footprints"][0]
    assert footprint["op_index"] == 0 and footprint["type"] == "section" and footprint["key"] == "contacts"
    assert footprint["base_text"].startswith("## Contacts\n- Jane Rivera")
    assert SAM in footprint["current_text"] and SAM not in footprint["base_text"]
    assert not footprint["base_text"].endswith("\n")
    assert "Nothing was written" in error.message
    assert tree_snapshot(lib.root) == before
    assert lib.history() == []
    assert lib.leftovers() == []


def test_spec9_step6_contacts_conflict(lib):
    base = lib.base()
    lib.edit("jane@acme-bakery.example | email", "jane@acme-bakery.example | phone")
    error = conflict_of(lib, base, [op_contacts_replace()])
    assert error.details["footprints"][0]["key"] == "contacts"
    assert "email preferred" in error.details["footprints"][0]["base_text"]
    assert "phone" in error.details["footprints"][0]["current_text"]


def test_spec9_step6_communication_preferences_rebases(lib):
    base = lib.base()
    lib.edit("jane@acme-bakery.example | email", "jane@acme-bakery.example | phone")
    result = do_update(lib, base, [op_comm_append()])
    assert result.data["rebased"] is True
    out = lib.read().decode()
    assert "| phone" in out and "- Monthly review call" in out


def test_spec9_step6_stale_status_after_external_status_edit_conflicts(lib):
    base = lib.base()
    lib.edit("status: active", "status: paused")
    error = conflict_of(lib, base, [op_status("former")])
    footprint = error.details["footprints"][0]
    assert (footprint["type"], footprint["key"], footprint["op_index"]) == ("property", "status", 0)
    assert footprint["base_text"] == "status: active\n"
    assert footprint["current_text"] == "status: paused\n"
    assert parse(lib.read()).properties["status"] == "paused"


def test_all_footprints_are_compared_before_any_op_applies(lib):
    base = lib.base()
    lib.edit("status: active", "status: paused")
    lib.edit("## Contacts\n", "## Contacts\n" + SAM + "\n")
    error = conflict_of(lib, base, [op_status("former"), op_comm_append(), op_contacts_replace()])
    keys = [(f["op_index"], f["key"]) for f in error.details["footprints"]]
    assert keys == [(0, "status"), (2, "contacts")]  # op 1 is unchanged and not listed


def test_later_ops_are_not_compared_against_intermediate_states(lib):
    base = lib.base()
    lib.edit("## Locations\n", "## Locations\n- Brand assets: https://drive.example/acme-brand\n")
    ops = [
        {"op": "append_to_section", "section": "Contacts", "text": SAM},
        {"op": "replace_line", "section": "Contacts", "match": "sam@acme-bakery.example", "text": SAM + " | cell"},
    ]
    # Op 1 matches a line that only exists after op 0; op 0's edit must not count as drift.
    result = do_update(lib, base, ops)
    assert result.data["rebased"] is True
    assert "| cell" in lib.read().decode()


def test_section_absent_in_both_is_not_a_conflict(lib):
    lib.path.write_bytes(lib.read().replace(b"## History\n\n", b""))
    base = lib.base()
    lib.edit("status: active", "status: paused")
    result = do_update(lib, base, [{"op": "append_to_section", "section": "History", "text": "- 2019: opened"}])
    assert result.data["rebased"] is True
    assert "## History\n- 2019: opened" in lib.read().decode()


def test_section_created_externally_since_base_conflicts_with_null_base_text(lib):
    lib.path.write_bytes(lib.read().replace(b"## History\n\n", b""))
    base = lib.base()
    lib.edit("## Unconfirmed", "## History\n- external\n\n## Unconfirmed")
    error = conflict_of(lib, base, [{"op": "append_to_section", "section": "History", "text": "- mine"}])
    footprint = error.details["footprints"][0]
    assert footprint["base_text"] is None and footprint["current_text"] == "## History\n- external"


def test_property_footprint_includes_a_list_span(lib):
    base = lib.base()
    lib.edit("aliases: [Acme]", "aliases: [Acme, Acme Co]")
    error = conflict_of(lib, base, [{"op": "set_property", "key": "aliases", "values": ["ACME"]}])
    footprint = error.details["footprints"][0]
    assert footprint["base_text"] == "aliases: [Acme]\n" and footprint["current_text"] == "aliases: [Acme, Acme Co]\n"


def test_updated_and_change_log_edits_never_count_as_conflicts(lib):
    base = lib.base()
    lib.edit("updated: 2026-11-05", "updated: 2026-11-06")
    lib.edit("created from onboarding conversation\n", "created from onboarding conversation\n- 2026-11-06 owner: manual note\n")
    result = do_update(lib, base, [op_comm_append()])
    assert result.data["rebased"] is True
    out = lib.read().decode()
    assert "owner: manual note" in out and result.data["change_log_line"] in out
    assert parse(lib.read()).properties["updated"] == "2026-11-07"


def test_external_edit_of_an_unrelated_property_is_kept(lib):
    base = lib.base()
    lib.edit("aliases: [Acme]", "aliases: [Acme, Acme Co]")
    do_update(lib, base, [op_status("paused")])
    assert parse(lib.read()).properties["aliases"] == ["Acme", "Acme Co"]


def test_rebase_footprints_survive_a_crlf_current(tmp_path):
    lib = make_library(tmp_path)
    base = lib.base()
    crlf = ACME.replace("\n", "\r\n").encode()
    lib.path.write_bytes(crlf)  # editor re-saved with CRLF: every footprint is EOL-normalised equal
    result = do_update(lib, base, [op_comm_append()])
    assert result.data["rebased"] is True
    assert b"\r\n" in lib.read() and b"\n" not in lib.read().replace(b"\r\n", b"")


# ---- base_unavailable -----------------------------------------------------

def test_missing_base_snapshot_is_base_unavailable(lib):
    base = lib.base()
    lib.edit("status: active", "status: paused")
    (lib.meta / "snapshots" / f"{base}.md").unlink()
    before = tree_snapshot(lib.root)
    error = conflict_of(lib, base, [op_comm_append()])
    assert error.details == {"reason": "base_unavailable", "current_rev": rev(lib.read()), "footprints": []}
    assert tree_snapshot(lib.root) == before


def test_corrupt_base_snapshot_is_base_unavailable(lib):
    base = lib.base()
    lib.edit("status: active", "status: paused")
    (lib.meta / "snapshots" / f"{base}.md").write_bytes(ACME.encode() + b"!")
    error = conflict_of(lib, base, [op_comm_append()])
    assert error.details["reason"] == "base_unavailable"
    assert lib.history() == []


def test_never_read_rev_is_base_unavailable(lib):
    error = conflict_of(lib, "0123456789abcdef", [op_comm_append()])
    assert error.details["reason"] == "base_unavailable"


def test_same_rev_needs_no_snapshot(lib):
    base = lib.base()
    (lib.meta / "snapshots" / f"{base}.md").unlink()
    assert do_update(lib, base, [op_comm_append()]).data["rebased"] is False


# ---- request validation and stage order ------------------------------------

@pytest.mark.parametrize("base_rev", ["", "abc", "ABCDEF0123456789", "0123456789abcdeg", None, 5])
def test_bad_base_rev_is_invalid_op(lib, base_rev):
    with pytest.raises(EngagementsError) as exc:
        do_update(lib, base_rev, [op_status()])
    assert exc.value.code == "invalid_op" and exc.value.details["field"] == "base_rev"


@pytest.mark.parametrize("source", ["", "  ", "two\nlines", "x" * 201, None])
def test_bad_source_is_invalid_op(lib, source):
    with pytest.raises(EngagementsError) as exc:
        do_update(lib, lib.base(), [op_status()], source=source)
    assert exc.value.code == "invalid_op" and exc.value.details["field"] == "source"


def test_source_with_a_closing_paren_is_kept_as_is(lib):
    data = do_update(lib, lib.base(), [op_status()], source="call (notes) done)").data
    assert data["change_log_line"].endswith("(source: call (notes) done))")


def test_shape_errors_come_before_the_file_is_read(lib):
    with pytest.raises(EngagementsError) as exc:
        do_update(lib, lib.base(), [{"op": "set_property", "key": "updated", "value": "2026-01-01"}],
                  path=lib.path.with_name("Missing.md"))
    assert exc.value.details["reason"] == "plugin_owned"


def test_missing_record_is_not_found(lib):
    with pytest.raises(EngagementsError) as exc:
        do_update(lib, lib.base(), [op_status()], path=lib.path.with_name("Missing.md"))
    assert exc.value.code == "not_found"


def test_a_file_that_is_not_a_record_is_not_found(lib):
    other = lib.root / "Note.md"
    other.write_text("# just a note\n")
    with pytest.raises(EngagementsError) as exc:
        do_update(lib, "0" * 16, [op_status()], path=other)
    assert (exc.value.code, exc.value.details["reason"]) == ("not_found", "not_a_record")


def test_outside_root_path_is_refused_and_untouched(lib, tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_bytes(ACME.encode())
    with pytest.raises(EngagementsError) as exc:
        do_update(lib, rev(ACME.encode()), [op_status()], path=outside)
    assert exc.value.code == "outside_root" and outside.read_bytes() == ACME.encode()


def test_value_errors_are_invalid_op_and_write_nothing(lib):
    base = lib.base()
    before = tree_snapshot(lib.root)
    with pytest.raises(EngagementsError) as exc:
        do_update(lib, base, [op_status("historical")])
    assert exc.value.details["reason"] == "status_not_allowed_for_kind"
    assert tree_snapshot(lib.root) == before


def test_apply_errors_are_surfaced_with_nothing_written(lib):
    base = lib.base()
    before = lib.read()
    with pytest.raises(EngagementsError) as exc:
        do_update(lib, base, [{"op": "replace_line", "section": "Contacts", "match": "no such line", "text": "x"}])
    assert exc.value.code == "invalid_op" and exc.value.details["reason"] == "match_count"
    assert lib.read() == before and lib.history() == [] and lib.leftovers() == []


def test_unknown_kind_argument_is_a_caller_bug(lib):
    with pytest.raises(ValueError):
        do_update(lib, lib.base(), [op_status()], kind="other")


# ---- global defects at stage 4, before the lock -----------------------------

def test_global_defect_is_malformed_with_the_path(tmp_path):
    lib = make_library(tmp_path, ACME.replace("id: acme-bakery\n", "id: acme-bakery\nid: other-id\n"))
    with pytest.raises(EngagementsError) as exc:
        do_update(lib, "0" * 16, [op_status()])
    assert exc.value.code == "malformed"
    assert exc.value.details["path"] == "Acme Bakery/Client.md"
    assert exc.value.details["defects"]


def test_malformed_wins_over_a_value_error(tmp_path):
    lib = make_library(tmp_path, ACME.replace("name: Acme Bakery\n", ""))
    with pytest.raises(EngagementsError) as exc:
        do_update(lib, "0" * 16, [op_status("historical")])
    assert exc.value.code == "malformed"


def test_targeted_defect_from_apply_carries_the_path(tmp_path):
    lib = make_library(tmp_path, ACME.replace("## Summary\n", "## Summary\nx\n\n## Summary\n", 1))
    base = lib.base()
    with pytest.raises(EngagementsError) as exc:
        do_update(lib, base, [{"op": "append_to_section", "section": "Summary", "text": "more"}])
    assert exc.value.code == "malformed" and exc.value.details["path"] == "Acme Bakery/Client.md"


def test_library_defect_duplicate_id_refuses(lib):
    with pytest.raises(EngagementsError) as exc:
        do_update(lib, lib.base(), [op_status()], library_defects=lambda record: {"duplicate_id"})
    assert exc.value.code == "malformed"
    assert {d["kind"] for d in exc.value.details["defects"]} == {"duplicate_id"}


def test_library_defect_callback_gets_the_parsed_record(lib):
    seen = []
    do_update(lib, lib.base(), [op_status()], library_defects=lambda record: seen.append(record.id) or ())
    assert seen == ["acme-bakery", "acme-bakery"]  # pre-lock bytes, then the locked bytes


def test_unexpected_library_defect_is_a_caller_bug(lib):
    with pytest.raises(ValueError):
        do_update(lib, lib.base(), [op_status()], library_defects=lambda record: {"mixed_eol"})


def test_unknown_client_blocks_ordinary_edits(tmp_path):
    lib = make_library(tmp_path, _project("missing-client"), name="Website 2026.md", folder="Projects")
    with pytest.raises(EngagementsError) as exc:
        writer.update(lib.root, lib.path, "0" * 16, "s", [{"op": "append_to_section", "section": "Notes", "text": "x"}],
                      kind="project", profile="p", clock=fixed_clock,
                      client_exists=lambda c: c == "acme-bakery")
    assert exc.value.code == "malformed"
    assert {d["kind"] for d in exc.value.details["defects"]} == {"unknown_client"}


def test_spec_c5_repair_of_unknown_client_is_allowed(tmp_path):
    lib = make_library(tmp_path, _project("missing-client"), name="Website 2026.md", folder="Projects")
    base = writer.read_record(lib.root, lib.path)[1]
    result = writer.update(lib.root, lib.path, base, "fix", [{"op": "set_property", "key": "client", "value": "acme-bakery"}],
                           kind="project", profile="p", clock=fixed_clock, client_exists=lambda c: c == "acme-bakery")
    assert parse(lib.read()).properties["client"] == "acme-bakery"
    assert result.data["change_log_line"].endswith("p: set client (source: fix)")


def _project(client):
    return WEBSITE.replace("client: acme-bakery", f"client: {client}")
