"""Commit step (spec 5.3 steps 5-7): hooks, history, metadata failures, post-replace warnings."""
import shutil

import pytest
from conftest import patch_replace
from engagements import writer
from engagements.errors import EngagementsError
from engagements.fsutil import rev
from engagements.records import parse
from writer_support import (
    ACME,
    do_update,
    fixed_clock,
    make_library,
    op_comm_append,
    op_status,
    ticking_clock,
)

EDITED = b"\n<!-- an editor save -->\n"


# ---- the final re-hash and the documented hooks ------------------------------

def test_before_final_rehash_edit_is_changed_during_write(lib):
    base = lib.base()
    original = lib.read()
    seen = {}

    def hook():
        seen["temps"] = lib.leftovers()  # the temp file exists, fsynced, before the re-hash
        lib.path.write_bytes(original + EDITED)

    with pytest.raises(EngagementsError) as exc:
        do_update(lib, base, [op_status()], before_final_rehash=hook)
    assert exc.value.code == "conflict"
    assert exc.value.details == {"reason": "changed_during_write", "current_rev": rev(original + EDITED), "footprints": []}
    assert len(seen["temps"]) == 1  # proves the hook ran after the temp write
    assert lib.read() == original + EDITED  # the editor's bytes, untouched
    assert lib.leftovers() == []  # temp removed


def test_before_final_rehash_delete_is_changed_during_write(lib):
    base = lib.base()
    with pytest.raises(EngagementsError) as exc:
        do_update(lib, base, [op_status()], before_final_rehash=lambda: lib.path.unlink())
    assert exc.value.code == "conflict" and exc.value.details["reason"] == "changed_during_write"
    assert exc.value.details["current_rev"] is None
    assert lib.leftovers() == []


def test_after_final_rehash_edit_is_the_documented_overwrite_window(lib):
    base = lib.base()
    original = lib.read()
    result = do_update(lib, base, [op_status("paused")], after_final_rehash=lambda: lib.path.write_bytes(original + EDITED))
    assert result.data["rebased"] is False  # no conflict is expected
    assert EDITED not in lib.read()  # the editor's save is lost (spec 5.5)
    assert lib.read() != original + EDITED
    assert result.data["new_rev"] == rev(lib.read())
    history = lib.history()
    assert len(history) == 1 and history[0].read_bytes() == original  # bytes the plugin read, not the save
    assert lib.leftovers() == []


def test_hooks_are_called_in_order_with_no_arguments(lib):
    calls = []
    do_update(lib, lib.base(), [op_status()], before_final_rehash=lambda: calls.append("before"),
              after_final_rehash=lambda: calls.append("after"))
    assert calls == ["before", "after"]


def test_a_conflict_before_replace_is_not_reported_by_the_after_hook(lib):
    calls = []
    with pytest.raises(EngagementsError):
        do_update(lib, lib.base(), [op_status()], before_final_rehash=lambda: lib.path.write_bytes(b"x"),
                  after_final_rehash=lambda: calls.append("after"))
    assert calls == []


def test_replace_failure_is_io_error_and_leaves_the_record_untouched(lib, monkeypatch):
    base = lib.base()

    def fail(*args, **kwargs):
        raise OSError(30, "read-only filesystem /secret")

    patch_replace(monkeypatch, fail)
    with pytest.raises(EngagementsError) as exc:
        do_update(lib, base, [op_status()])
    assert exc.value.code == "io_error" and "secret" not in str(exc.value.details) + exc.value.message
    assert lib.read() == ACME.encode() and lib.leftovers() == []


def test_temp_write_failure_is_io_error_and_cleans_up(lib, monkeypatch):
    base = lib.base()
    real = writer.os.fsync
    directory = str(lib.path.parent)

    def flaky(fd):
        import os
        if os.path.dirname(os.readlink(f"/proc/self/fd/{fd}")) == directory:
            raise OSError(5, "io")  # only the temp file beside the record
        return real(fd)

    monkeypatch.setattr(writer.os, "fsync", flaky)
    with pytest.raises(EngagementsError) as exc:
        do_update(lib, base, [op_status()])
    assert exc.value.code == "io_error"
    assert lib.read() == ACME.encode() and lib.leftovers() == []


# ---- history ---------------------------------------------------------------

def test_history_holds_the_prior_bytes_under_a_utc_name(lib):
    base = lib.base()
    do_update(lib, base, [op_status()])
    (entry,) = lib.history()
    assert entry.read_bytes() == ACME.encode()
    # clock is 2026-11-07 14:02 at UTC-06:00, so the UTC stamp is 20:02
    assert entry.name == f"20261107T200200.000000Z-{rev(ACME.encode())}.md"


def test_history_keeps_exactly_the_last_50(lib):
    clock = ticking_clock()
    revision = lib.base()
    for number in range(60):
        revision = do_update(lib, revision, [{"op": "append_to_section", "section": "Summary", "text": f"n{number}"}],
                             clock=clock).data["new_rev"]
    entries = lib.history()
    assert len(entries) == 50
    # Updates 0..9 are pruned. Update k's entry holds the bytes before it, which end at append n(k-1).
    first_kept = entries[0].read_bytes().decode()
    assert "n9\n" in first_kept and "n10" not in first_kept
    last_kept = entries[-1].read_bytes().decode()
    assert "n58\n" in last_kept and "n59" not in last_kept


def test_history_is_per_record(tmp_path):
    lib = make_library(tmp_path)
    other = lib.root / "Acme Bakery" / "Projects"
    other.mkdir()
    (other / "Website.md").write_text(
        "---\nengagements: project\nid: acme-website-2026\nclient: acme-bakery\nname: Website 2026\nstatus: active\n"
        "updated: 2026-11-05\n---\n\n## Notes\n\n## Change log\n")
    do_update(lib, lib.base(), [op_status()])
    base = writer.read_record(lib.root, other / "Website.md")[1]
    writer.update(lib.root, other / "Website.md", base, "s", [{"op": "append_to_section", "section": "Notes", "text": "x"}],
                  kind="project", profile="p", clock=fixed_clock,
                  client_exists=lambda c: True)
    assert sorted(p.name for p in (lib.meta / "history").iterdir()) == ["acme-bakery", "acme-website-2026"]  # directory order is not defined


@pytest.mark.parametrize("breaker", ["history_link", "meta_link", "id_dir_is_file", "history_is_file"])
def test_history_write_failure_is_io_error_and_the_record_is_untouched(lib, tmp_path, breaker):
    base = lib.base()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    if breaker == "history_link":
        (lib.meta / "history").symlink_to(elsewhere)
    elif breaker == "meta_link":
        moved = lib.root / "moved-meta"
        lib.meta.rename(moved)
        (lib.root / ".engagements").symlink_to(moved)
    elif breaker == "id_dir_is_file":
        (lib.meta / "history").mkdir()
        (lib.meta / "history" / "acme-bakery").write_text("not a directory")
    else:
        (lib.meta / "history").write_text("not a directory")
    before = lib.read()
    with pytest.raises(EngagementsError) as exc:
        do_update(lib, base, [op_status()])
    assert exc.value.code == "io_error"
    assert exc.value.details["path"].startswith(".engagements")  # relative, inside the root
    assert lib.read() == before and lib.leftovers() == []
    assert list(elsewhere.iterdir()) == []  # nothing followed a symlink


def test_history_failure_message_has_no_absolute_paths(lib, tmp_path):
    base = lib.base()
    (lib.meta / "history").symlink_to(tmp_path)
    with pytest.raises(EngagementsError) as exc:
        do_update(lib, base, [op_status()])
    assert str(tmp_path) not in exc.value.message + str(exc.value.details)


# ---- failures after the replace are warnings -------------------------------

def test_directory_fsync_failure_after_replace_is_ok_with_a_warning(lib, monkeypatch):
    base = lib.base()
    real = writer._fsync_dir
    failed = []

    def fail(directory):
        # Only the record directory's sync after the replace; the required
        # history copy's sync is checked before it.
        if directory.relpath.startswith(".engagements"):
            return real(directory)
        failed.append(directory.relpath)
        raise OSError(5, "dir sync failed")

    monkeypatch.setattr(writer, "_fsync_dir", fail)
    result = do_update(lib, base, [op_status("paused")])
    assert failed
    assert parse(lib.read()).properties["status"] == "paused"
    assert [w["code"] for w in result.warnings] == ["maintenance_warning"]
    assert result.data["new_rev"] == rev(lib.read())


def test_snapshot_failure_after_replace_is_ok_with_a_warning(lib, tmp_path):
    base = lib.base()
    shutil.rmtree(lib.meta / "snapshots")
    (lib.meta / "snapshots").symlink_to(tmp_path)
    result = do_update(lib, base, [op_status("paused")])
    assert parse(lib.read()).properties["status"] == "paused"
    assert [w["code"] for w in result.warnings] == ["maintenance_warning"]
    assert list(tmp_path.glob("*.md")) == []


def test_history_pruning_failure_after_replace_is_ok_with_a_warning(lib, monkeypatch):
    base = lib.base()

    def fail(*args, **kwargs):
        raise OSError(5, "cannot prune")

    monkeypatch.setattr(writer, "_prune_history", fail)
    result = do_update(lib, base, [op_status("paused")])
    assert [w["code"] for w in result.warnings] == ["maintenance_warning"]
    assert parse(lib.read()).properties["status"] == "paused"


def test_snapshot_pruning_failure_after_replace_is_ok_with_a_warning(lib, monkeypatch):
    base = lib.base()

    def fail(*args, **kwargs):
        raise OSError(5, "cannot prune")

    monkeypatch.setattr(writer, "prune_snapshots", fail)
    result = do_update(lib, base, [op_status("paused")])
    assert [w["code"] for w in result.warnings] == ["maintenance_warning"]


def test_lock_release_failure_after_commit_is_a_warning_not_an_error(lib, monkeypatch):
    base = lib.base()
    real_exit = writer.FileLock.__exit__

    def bad_exit(self, *args):
        real_exit(self, *args)
        raise EngagementsError("io_error", "Filesystem operation failed.", {"path": "x"})

    monkeypatch.setattr(writer.FileLock, "__exit__", bad_exit)
    result = do_update(lib, base, [op_status("paused")])
    assert [w["code"] for w in result.warnings] == ["maintenance_warning"]
    assert parse(lib.read()).properties["status"] == "paused"


def test_lock_release_failure_does_not_mask_a_real_error(lib, monkeypatch):
    base = lib.base()
    real_exit = writer.FileLock.__exit__

    def bad_exit(self, *args):
        real_exit(self, *args)
        raise EngagementsError("io_error", "Filesystem operation failed.", {"path": "x"})

    monkeypatch.setattr(writer.FileLock, "__exit__", bad_exit)
    with pytest.raises(EngagementsError) as exc:
        do_update(lib, base, [{"op": "remove_line", "section": "Contacts", "match": "nobody"}])
    assert exc.value.code == "invalid_op"


def test_nothing_is_added_to_warnings_on_a_clean_update(lib):
    assert do_update(lib, lib.base(), [op_comm_append()]).warnings == []


def test_metadata_after_success_is_locks_snapshots_and_history_only(lib):
    do_update(lib, lib.base(), [op_status()])
    assert sorted(p.name for p in lib.meta.iterdir()) == ["history", "library.yaml", "locks", "snapshots"]
    assert [p.name for p in (lib.meta / "locks").iterdir()] == ["acme-bakery.lock"]
    stray = list(lib.root.rglob(".engagements-*"))
    assert stray == []  # no temp file left anywhere in the tree


def test_directory_fsync_unsupported_is_silent(lib, monkeypatch):
    import errno
    import os
    import stat as stat_module

    real = os.fsync

    def fsync(fd):
        if stat_module.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "unsupported")
        return real(fd)

    base = lib.base()
    monkeypatch.setattr(writer.os, "fsync", fsync)
    result = do_update(lib, base, [op_status("paused")])
    assert result.warnings == []


def test_a_kind_that_no_longer_matches_the_file_is_footprint_changed(lib):
    base = lib.base()
    with pytest.raises(EngagementsError) as exc:
        do_update(lib, base, [op_status("active")], kind="project")
    assert exc.value.code == "conflict"
    (footprint,) = exc.value.details["footprints"]
    assert footprint["key"] == "engagements" and footprint["current_text"] == "engagements: client\n"
    assert lib.read() == ACME.encode()


def test_a_multiline_profile_is_a_caller_bug(lib):
    with pytest.raises(ValueError):
        do_update(lib, lib.base(), [op_status()], profile="two\nlines")


def test_locks_directory_symlink_is_io_error_before_anything_is_written(lib, tmp_path):
    base = lib.base()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (lib.meta / "locks").symlink_to(elsewhere)
    with pytest.raises(EngagementsError) as exc:
        do_update(lib, base, [op_status()])
    assert exc.value.code == "io_error" and exc.value.details["path"] == ".engagements/locks"
    assert list(elsewhere.iterdir()) == [] and lib.read() == ACME.encode() and lib.history() == []
