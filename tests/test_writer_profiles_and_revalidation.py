"""Two profiles on one root, and locked re-validation (TOOLS.md "Error precedence")."""
import pytest
from engagements import writer
from engagements.errors import EngagementsError
from engagements.fsutil import FileLock, rev
from engagements.records import parse
from writer_support import (
    SAM,
    WEBSITE,
    do_update,
    fixed_clock,
    make_library,
    op_comm_append,
    op_contacts_replace,
    op_status,
)

# ---- profile A reads, profile B updates, profile A updates from its stale rev ----


def test_profile_a_rebases_over_profile_bs_disjoint_update(lib):
    a_rev = writer.read_record(lib.root, lib.path)[1]  # profile A reads
    b = do_update(lib, a_rev, [{"op": "append_to_section", "section": "Contacts", "text": SAM}], profile="studio-ops")
    assert b.data["rebased"] is False
    a = do_update(lib, a_rev, [op_comm_append()], profile="studio-dev")  # profile A, stale rev
    assert a.data["rebased"] is True
    text = lib.read().decode()
    assert SAM in text and "- Monthly review call" in text
    assert "studio-ops: append to Contacts" in text and "studio-dev: append to Communication preferences" in text
    assert text.index("studio-ops:") < text.index("studio-dev:")


def test_profile_a_conflicts_with_profile_bs_overlapping_update(lib):
    a_rev = writer.read_record(lib.root, lib.path)[1]
    do_update(lib, a_rev, [{"op": "append_to_section", "section": "Contacts", "text": SAM}], profile="studio-ops")
    on_disk = lib.read()
    with pytest.raises(EngagementsError) as exc:
        do_update(lib, a_rev, [op_contacts_replace()], profile="studio-dev")
    assert exc.value.code == "conflict" and exc.value.details["reason"] == "footprint_changed"
    assert SAM in exc.value.details["footprints"][0]["current_text"]
    assert lib.read() == on_disk  # profile B's change is intact
    assert len(lib.history()) == 1


def test_profile_a_stale_status_conflicts_with_profile_bs_status(lib):
    a_rev = writer.read_record(lib.root, lib.path)[1]
    do_update(lib, a_rev, [op_status("paused")], profile="studio-ops")
    with pytest.raises(EngagementsError) as exc:
        do_update(lib, a_rev, [op_status("former")], profile="studio-dev")
    assert exc.value.details["footprints"][0]["current_text"] == "status: paused\n"


def test_profile_a_can_retry_from_the_new_rev_after_a_conflict(lib):
    a_rev = writer.read_record(lib.root, lib.path)[1]
    do_update(lib, a_rev, [op_status("paused")], profile="studio-ops")
    with pytest.raises(EngagementsError):
        do_update(lib, a_rev, [op_status("former")], profile="studio-dev")
    fresh = writer.read_record(lib.root, lib.path)[1]
    result = do_update(lib, fresh, [op_status("former")], profile="studio-dev")
    assert result.data["rebased"] is False
    assert parse(lib.read()).properties["status"] == "former"


def test_default_profile_name_is_used_when_the_profile_is_unknown(lib):
    line = do_update(lib, lib.base(), [op_status()], profile=None).data["change_log_line"]
    assert " default: set status " in line


# ---- locked re-validation -------------------------------------------------------


@pytest.fixture
def after_lock(monkeypatch):
    """Run an external edit after the lock is held, before the locked read."""
    actions = []

    class Hooked(FileLock):
        def __enter__(self):
            result = super().__enter__()
            for action in actions:
                action()
            actions.clear()
            return result

    monkeypatch.setattr(writer, "FileLock", Hooked)
    return actions.append


def raising(lib, base, ops):
    with pytest.raises(EngagementsError) as exc:
        do_update(lib, base, ops)
    return exc.value


def test_locked_read_that_removes_name_is_malformed_even_if_the_footprint_is_unchanged(lib, after_lock):
    base = lib.base()
    after_lock(lambda: lib.edit("name: Acme Bakery\n", ""))
    error = raising(lib, base, [op_comm_append()])
    assert error.code == "malformed"
    assert {d["kind"] for d in error.details["defects"]} == {"missing_field"}
    assert error.details["path"] == "Acme Bakery/Client.md"
    assert b"name:" not in lib.read()  # untouched, as the editor left it


def test_locked_read_with_mixed_line_endings_is_malformed(lib, after_lock):
    base = lib.base()
    after_lock(lambda: lib.edit("## Summary\n", "## Summary\r\n"))
    error = raising(lib, base, [op_comm_append()])
    assert error.code == "malformed"
    assert {d["kind"] for d in error.details["defects"]} == {"mixed_eol"}


def test_malformed_wins_over_the_identity_change_conflict(lib, after_lock):
    base = lib.base()

    def edit():
        lib.edit("id: acme-bakery\n", "id: acme-other\n")
        lib.edit("name: Acme Bakery\n", "")

    after_lock(edit)
    error = raising(lib, base, [op_comm_append()])
    assert error.code == "malformed"


def test_malformed_wins_even_with_a_changed_footprint(lib, after_lock):
    base = lib.base()
    after_lock(lambda: (lib.edit("name: Acme Bakery\n", ""), lib.edit("## Contacts\n", "## Contacts\n" + SAM + "\n")))
    assert raising(lib, base, [op_contacts_replace()]).code == "malformed"


def test_identity_change_without_a_global_defect_is_footprint_changed_on_id(lib, after_lock):
    base = lib.base()
    after_lock(lambda: lib.edit("id: acme-bakery\n", "id: acme-bakery-2\n"))
    error = raising(lib, base, [op_comm_append()])
    assert error.code == "conflict" and error.details["reason"] == "footprint_changed"
    assert error.details["current_rev"] == rev(lib.read())
    (footprint,) = error.details["footprints"]
    assert footprint["type"] == "property" and footprint["key"] == "id"
    assert footprint["base_text"] == "id: acme-bakery\n" and footprint["current_text"] == "id: acme-bakery-2\n"
    assert footprint["op_index"] is None  # SPEC-QUESTION: TOOLS.md names no op for an identity footprint


def test_kind_change_between_reads_is_footprint_changed_on_engagements(lib, after_lock):
    base = lib.base()
    after_lock(lambda: lib.edit("engagements: client\n", "engagements: project\nclient: riverbend-dental\n"))
    error = raising(lib, base, [op_comm_append()])
    assert error.code == "conflict" and error.details["reason"] == "footprint_changed"
    (footprint,) = error.details["footprints"]
    assert footprint["key"] == "engagements"
    assert footprint["base_text"] == "engagements: client\n" and footprint["current_text"] == "engagements: project\n"


def test_a_client_turned_project_without_a_client_field_is_malformed(lib, after_lock):
    base = lib.base()
    after_lock(lambda: lib.edit("engagements: client\n", "engagements: project\n"))
    error = raising(lib, base, [op_comm_append()])
    assert error.code == "malformed"
    assert {d["kind"] for d in error.details["defects"]} == {"missing_field"}


def test_marker_removed_between_reads_is_footprint_changed_on_engagements(lib, after_lock):
    base = lib.base()
    after_lock(lambda: lib.edit("engagements: client\n", "engagements: nothing\n"))
    error = raising(lib, base, [op_comm_append()])
    assert error.code == "conflict"
    assert error.details["footprints"][0]["key"] == "engagements"
    assert error.details["footprints"][0]["current_text"] == "engagements: nothing\n"


def test_unchanged_identity_with_changed_other_bytes_is_not_an_identity_conflict(lib, after_lock):
    base = lib.base()
    after_lock(lambda: lib.edit("status: active", "status: paused"))
    result = do_update(lib, base, [op_comm_append()])
    assert result.data["rebased"] is True


def test_locked_read_that_gains_a_duplicate_id_is_refused_via_library_defects(lib, after_lock):
    base = lib.base()
    state = {"locked": False}
    after_lock(lambda: state.update(locked=True))

    def defects(record):
        return {"duplicate_id"} if state["locked"] else set()

    with pytest.raises(EngagementsError) as exc:
        do_update(lib, base, [op_comm_append()], library_defects=defects)
    assert exc.value.code == "malformed"
    assert {d["kind"] for d in exc.value.details["defects"]} == {"duplicate_id"}


def test_locked_read_that_loses_its_client_is_unknown_client(tmp_path, monkeypatch):
    lib = make_library(tmp_path, WEBSITE, name="Website 2026.md", folder="Projects")
    base = writer.read_record(lib.root, lib.path)[1]
    exists = {"value": True}

    def client_exists_now(value):
        return exists["value"] and value == "acme-bakery"

    class Hooked(FileLock):
        def __enter__(self):
            result = super().__enter__()
            exists["value"] = False  # the client record disappears while we wait
            return result

    monkeypatch.setattr(writer, "FileLock", Hooked)
    with pytest.raises(EngagementsError) as exc:
        writer.update(lib.root, lib.path, base, "s", [{"op": "append_to_section", "section": "Notes", "text": "x"}],
                      kind="project", profile="p", clock=fixed_clock, client_exists=client_exists_now)
    assert exc.value.code == "malformed"
    assert {d["kind"] for d in exc.value.details["defects"]} == {"unknown_client"}


def test_file_deleted_while_waiting_for_the_lock_is_not_found(lib, after_lock):
    base = lib.base()
    after_lock(lambda: lib.path.unlink())
    assert raising(lib, base, [op_comm_append()]).code == "not_found"


def test_locked_revalidation_writes_nothing(lib, after_lock):
    base = lib.base()
    after_lock(lambda: lib.edit("name: Acme Bakery\n", ""))
    with pytest.raises(EngagementsError):
        do_update(lib, base, [op_comm_append()])
    assert b"name:" not in lib.read() and b"Monthly review call" not in lib.read()
    assert lib.history() == [] and lib.leftovers() == []
