"""Every writer mutation goes through directories held open by descriptor, and an opened
library's root identity is honoured, so a directory or root swapped after validation cannot
redirect a write. Fictional data only.

The writer used to validate metadata and record directories by path and then write
through those paths, so a directory swapped for a symlink after validation
redirected the lock, history, snapshot or record write. Every write now goes
through directories held open by descriptor, each opened without following a
link, from the opened root down.

The writer used to re-resolve the library root with ``realpath`` on every call, so
a root replaced by a symlink after the library was opened moved the write
boundary. The opened root is now honored: its last component is never
followed, its pathname must still be canonical, and a ``Library`` carries the
root's identity so a swapped ancestor is refused too.

The swaps model another local writer racing the plugin. They use deterministic
hooks at the point of the swap, not timing.
"""

import os
import shutil

import pytest
from conftest import FIXTURE_LIBRARY
from engagements import fsutil, writer
from engagements import library as library_module
from engagements.errors import EngagementsError
from engagements.fsutil import FileLock, rev
from engagements.library import open_library
from engagements.records import parse
from writer_support import do_update, fixed_clock, make_library, op_status

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX symlink swaps")

ACME = "Acme Bakery/Client.md"


@pytest.fixture
def outside(tmp_path):
    path = tmp_path / "outside"
    path.mkdir()
    return path


def files_under(path):
    found = []
    for directory, _, names in os.walk(path):
        found.extend(os.path.relpath(os.path.join(directory, n), path) for n in names)
    return sorted(found)


def swap_after_meta_dir(monkeypatch, wanted, swap):
    """Run ``swap`` once, immediately after the writer validates ``.engagements/<wanted>``."""
    real = writer._meta_dir
    done = []

    def hooked(*args):
        result = real(*args)
        if tuple(args[1:]) == wanted and not done:
            done.append(True)
            swap()
        return result

    monkeypatch.setattr(writer, "_meta_dir", hooked)
    return done


# ---- held metadata directories -------------------------------------------------


def test_locks_swapped_after_validation_cannot_bypass_the_held_record_lock(tmp_path, outside, monkeypatch):
    lib = make_library(tmp_path)
    base = lib.base()
    locks = lib.meta / "locks"
    locks.mkdir()
    original = lib.read()

    def swap():
        os.rename(locks, lib.meta / "saved-locks")
        os.symlink(outside, locks)

    done = swap_after_meta_dir(monkeypatch, ("locks",), swap)
    # The lock is still held after the rename moves its file to saved-locks.
    with FileLock(locks / "acme-bakery.lock", timeout=1), pytest.raises(EngagementsError) as exc:
        do_update(lib, base, [op_status()], lock_timeout=0.05)
    assert done
    assert exc.value.code == "lock_timeout"
    assert files_under(outside) == []
    assert lib.read() == original and lib.history() == []


def test_history_swapped_after_validation_is_never_written_outside(tmp_path, outside, monkeypatch):
    lib = make_library(tmp_path)
    base = lib.base()
    history = lib.meta / "history" / "acme-bakery"
    original = lib.read()

    def swap():
        os.rename(history, lib.meta / "history" / "saved")
        os.symlink(outside, history)

    done = swap_after_meta_dir(monkeypatch, ("history", "acme-bakery"), swap)
    result = do_update(lib, base, [op_status()])
    assert done
    assert files_under(outside) == []
    # The copy went to the directory that was validated and held, still inside the library.
    saved = sorted((lib.meta / "history" / "saved").iterdir())
    assert [p.read_bytes() for p in saved] == [original]
    assert parse(lib.read()).properties["status"] == "paused"
    assert "maintenance_warning" in [w["code"] for w in result.warnings]  # pruning refuses the link


def test_snapshots_swapped_after_validation_are_never_written_outside(tmp_path, outside, monkeypatch):
    lib = make_library(tmp_path)
    base = lib.base()
    snapshots = lib.meta / "snapshots"

    def swap():
        os.rename(snapshots, lib.meta / "saved-snapshots")
        os.symlink(outside, snapshots)

    done = swap_after_meta_dir(monkeypatch, ("snapshots",), swap)
    result = do_update(lib, base, [op_status()])
    assert done
    assert files_under(outside) == []
    assert (lib.meta / "saved-snapshots" / f"{result.data['new_rev']}.md").read_bytes() == lib.read()


def test_record_directory_swapped_before_the_replace_is_never_written_outside(tmp_path, outside):
    # The same class on the record's own directory: the client folder is moved
    # inside the library and its old name pointed at an outside copy mid-commit.
    lib = make_library(tmp_path)
    base = lib.base()
    original = lib.read()
    folder = lib.path.parent
    moved = lib.root / "Acme Moved"
    copy = outside / "Acme Bakery"

    def swap():
        os.rename(folder, moved)
        copy.mkdir()
        shutil.copy2(moved / "Client.md", copy / "Client.md")
        os.symlink(copy, folder)

    do_update(lib, base, [op_status()], before_final_rehash=swap)
    assert (copy / "Client.md").read_bytes() == original
    assert files_under(outside) == ["Acme Bakery/Client.md"]
    assert parse((moved / "Client.md").read_bytes()).properties["status"] == "paused"
    assert sorted(p.name for p in moved.iterdir()) == ["Client.md"]  # no temp file left behind


def test_record_directory_swapped_before_the_lock_is_refused_or_held(tmp_path, outside, monkeypatch):
    # Swap the client folder while the update waits for the lock. The writer holds the
    # folder it validated, so it reads and writes the moved folder, never the outside copy.
    lib = make_library(tmp_path)
    base = lib.base()
    original = lib.read()
    folder = lib.path.parent
    moved = lib.root / "Acme Moved"
    copy = outside / "Acme Bakery"

    class Hooked(FileLock):
        def __enter__(self):
            result = super().__enter__()
            os.rename(folder, moved)
            shutil.copytree(moved, copy)
            os.symlink(copy, folder)
            return result

    monkeypatch.setattr(writer, "FileLock", Hooked)
    do_update(lib, base, [op_status()])
    assert (copy / "Client.md").read_bytes() == original
    assert parse((moved / "Client.md").read_bytes()).properties["status"] == "paused"


# ---- the opened root is honored ---------------------------------------------------


@pytest.fixture
def replaced_root(library_root, tmp_path):
    """An opened library whose root pathname now points at a sibling copy."""
    lib = open_library(library_root, create_default=False)
    base = writer.read_record(lib.root, ACME)[1]
    sibling = tmp_path / "sibling"
    shutil.copytree(lib.root, sibling)
    original = tmp_path / "original"
    os.rename(lib.root, original)
    os.symlink(sibling, lib.root)
    return lib, base, sibling, original


@pytest.fixture
def replaced_ancestor(tmp_path):
    """An opened library whose root's parent directory now points at a copy."""
    root = tmp_path / "home" / "library"
    root.parent.mkdir()
    shutil.copytree(FIXTURE_LIBRARY, root)
    lib = open_library(root, create_default=False)
    base = writer.read_record(lib.root, ACME)[1]
    copy = tmp_path / "copy"
    shutil.copytree(root.parent, copy)
    original = tmp_path / "home-original"
    os.rename(root.parent, original)
    os.symlink(copy, root.parent)
    return lib, base, copy / "library", original / "library"


def _update(root, base):
    return writer.update(root, ACME, base, "test", [op_status()], kind="client", profile="p",
                         clock=fixed_clock, client_exists=lambda value: True)


def _assert_root_changed(error):
    assert error.code == "io_error", (error.code, error.details)
    assert error.details["reason"] == "root_changed"
    assert not os.path.isabs(error.details["path"])


def _unchanged(*roots):
    return {str(r): (r / ACME).read_bytes() for r in roots}


@pytest.mark.parametrize("as_library", [False, True], ids=["root-path", "library"])
def test_update_refuses_a_root_replaced_by_a_symlink(replaced_root, as_library):
    lib, base, sibling, original = replaced_root
    before = _unchanged(sibling, original)
    with pytest.raises(EngagementsError) as exc:
        _update(lib if as_library else lib.root, base)
    _assert_root_changed(exc.value)
    assert _unchanged(sibling, original) == before


@pytest.mark.parametrize("as_library", [False, True], ids=["root-path", "library"])
def test_update_refuses_a_root_whose_ancestor_was_replaced(replaced_ancestor, as_library):
    lib, base, copy, original = replaced_ancestor
    before = _unchanged(copy, original)
    with pytest.raises(EngagementsError) as exc:
        _update(lib if as_library else lib.root, base)
    _assert_root_changed(exc.value)
    assert _unchanged(copy, original) == before


def test_read_record_refuses_a_replaced_root(replaced_root):
    lib, _, sibling, _ = replaced_root
    snapshots = sorted((sibling / ".engagements" / "snapshots").iterdir())
    with pytest.raises(EngagementsError) as exc:
        writer.read_record(lib.root, ACME)
    _assert_root_changed(exc.value)
    assert sorted((sibling / ".engagements" / "snapshots").iterdir()) == snapshots


def test_record_path_maps_a_replaced_root_to_a_domain_error(replaced_root):
    lib = replaced_root[0]
    with pytest.raises(EngagementsError) as exc:
        lib.record_path(ACME)
    _assert_root_changed(exc.value)


def test_discovery_refuses_a_replaced_root(replaced_root):
    lib = replaced_root[0]
    with pytest.raises(EngagementsError) as exc:
        lib.discover()
    _assert_root_changed(exc.value)


def test_create_refuses_a_replaced_ancestor(replaced_ancestor):
    lib, _, copy, original = replaced_ancestor
    before = files_under(copy), files_under(original)
    with pytest.raises(EngagementsError) as exc:
        lib.create("client", "Foo Co", profile="p", today=fixed_clock)
    _assert_root_changed(exc.value)
    assert (files_under(copy), files_under(original)) == before


# ---- controls: the normal paths still work --------------------------------------------


def test_update_through_an_opened_library_object(library_root):
    lib = open_library(library_root, create_default=False)
    data, base = writer.read_record(lib, ACME)
    result = _update(lib, base)
    assert result.data["path"] == ACME
    assert writer.load_snapshot(lib, result.data["new_rev"]) == (lib.root / ACME).read_bytes()
    assert rev(data) == base


def test_a_record_created_through_the_library_updates_through_the_writer(library_root):
    lib = open_library(library_root, create_default=False)
    created = lib.create("client", "Foo Co", profile="p", today=fixed_clock)
    _, base = writer.read_record(lib, created.path)
    result = writer.update(lib, created.path, base, "test", [op_status()], kind="client", profile="p",
                           clock=fixed_clock, client_exists=lambda value: True)
    assert result.data["id"] == created.id
    assert (lib.root / ".engagements" / "locks" / f"{created.id}.lock").is_file()


def test_record_path_and_discovery_still_work_on_a_stable_root(library_root):
    lib = open_library(library_root, create_default=False)
    assert lib.record_path(ACME) == ACME
    assert any(e.relpath == ACME for e in lib.discover().entries)


def test_the_held_record_lock_excludes_a_competing_writer_across_a_locks_swap(library_root, outside, monkeypatch):
    # The repro: the record lock is held through the writer's own primitive, then
    # the validated locks directory is swapped for a link before the writer opens it.
    lib = open_library(library_root, create_default=False)
    created = lib.create("client", "Foo Co", profile="p", today=fixed_clock)
    _, base = writer.read_record(lib, created.path)
    meta = library_root / ".engagements"

    def swap():
        os.rename(meta / "locks", meta / "saved-locks")
        os.symlink(outside, meta / "locks")

    done = swap_after_meta_dir(monkeypatch, ("locks",), swap)
    before = (library_root / created.path).read_bytes()
    with lib._meta_dir("locks") as locks, \
            library_module._held_lock(lib.root, locks, f"{created.id}.lock", 1.0), \
            pytest.raises(EngagementsError) as exc:
        writer.update(lib, created.path, base, "test", [op_status()], kind="client", profile="p",
                      clock=fixed_clock, client_exists=lambda value: True, lock_timeout=0.05)
    assert done
    assert exc.value.code == "lock_timeout"
    assert files_under(outside) == []
    assert (library_root / created.path).read_bytes() == before


# ---- platforms without the safe primitives (simulated) ---------------------------------


@pytest.mark.parametrize("gaps", [{"_RELATIVE": False}, {"_NOFOLLOW": 0}, {"_RELATIVE": False, "_NOFOLLOW": 0}],
                         ids=["no-dir_fd", "no-nofollow", "neither"])
def test_writer_fails_closed_and_still_reads_without_safe_primitives(tmp_path, monkeypatch, gaps):
    lib = make_library(tmp_path)
    base = rev(lib.read())
    monkeypatch.setattr(fsutil, "_HANDLES", False)  # simulated platform: neither descriptors nor Windows handles
    for name, value in gaps.items():
        monkeypatch.setattr(fsutil, name, value)
    before = files_under(lib.root)
    with pytest.raises(EngagementsError) as exc:
        do_update(lib, base, [op_status()])
    assert (exc.value.code, exc.value.details["reason"]) == ("io_error", "unsupported_platform")
    warnings = []
    data, revision = writer.read_record(lib.root, lib.path, warnings=warnings)
    assert (data, revision) == (lib.read(), base)
    assert [w["code"] for w in warnings] == ["snapshot_failed"]
    assert files_under(lib.root) == before  # nothing created: no lock, history or snapshot


# ---- descriptors ------------------------------------------------------------------


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="needs /proc/self/fd")
def test_no_descriptor_leaks_on_success_conflict_and_refusal(tmp_path, outside):
    lib = make_library(tmp_path)

    def count():
        return len(os.listdir("/proc/self/fd"))

    base = lib.base()
    start = count()
    first = do_update(lib, base, [op_status()]).data["new_rev"]
    with pytest.raises(EngagementsError):
        do_update(lib, "0123456789abcdef", [op_status("former")])  # base_unavailable
    os.rename(lib.meta / "history", lib.meta / "saved-history")
    os.symlink(outside, lib.meta / "history")
    with pytest.raises(EngagementsError):
        do_update(lib, first, [op_status("former")])  # history is a link: refused before the replace
    writer.read_record(lib.root, lib.path, clock=fixed_clock)
    assert count() == start
    assert files_under(outside) == []
