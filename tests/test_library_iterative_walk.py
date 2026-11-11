"""The discovery walk is iterative and keeps one held descriptor per level, and create
refuses an incomplete inventory. Fictional data only.

The first held-descriptor walk spent two Python frames per
directory (``_walk`` and ``_walk_child``), so a tree only a few hundred levels
deep -- far inside the filesystem path limit -- raised ``RecursionError`` from
``find`` and hid every record, including shallow ones. The walk is now an
explicit stack of held ``_Dir`` handles: the same order and the same
``skipped_symlinks`` / ``unreadable`` / ``oversize`` bookkeeping as the
recursive version, one descriptor per level, and every held descriptor closed
on success, on ``OSError`` and on a ``BaseException`` such as
``KeyboardInterrupt`` (TOOLS.md "Filesystem boundary").

``create`` used to treat an incomplete scan as a complete inventory. A record or
subtree the walk could not read hid its id, name and aliases, so ``create``
could allocate an id already in use and skip ``similar_exists`` entirely
(spec 4.4's single id namespace, TOOLS.md create C1). A create whose under-lock
inventory has unreadable candidates now refuses with ``io_error`` /
``reason: "incomplete_scan"`` and writes nothing. Intentional exclusions
(symlinks, oversize files, dot-directories) are not read failures and still
allow create.
"""

import errno
import os
from pathlib import Path

import pytest
from conftest import record_bytes, write
from engagements import library as library_module
from engagements.errors import EngagementsError
from engagements.fsutil import _Dir
from engagements.library import MAX_RECORD_BYTES, open_library
from engagements.records import parse
from writer_support import fixed_clock

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX descriptors and modes")
IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0


def open_fds():
    return len(os.listdir("/proc/self/fd"))


needs_proc = pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="needs /proc/self/fd")


def new_library(tmp_path, name="library"):
    root = tmp_path / name
    root.mkdir()
    return open_library(root, create_default=True)


def make_client(lib, name="Acme Bakery"):
    return lib.create("client", name, profile="studio-dev", today=fixed_clock)


def top_level(root):
    return sorted(os.listdir(root))


def refused(exc):
    return exc.code == "io_error" and exc.details.get("reason") == "incomplete_scan"


# --- deep trees --------------------------------------------------------------


@needs_proc
def test_a_six_hundred_level_tree_still_returns_the_shallow_record(tmp_path):
    """Each level used to cost two Python frames, so 600 levels raised RecursionError."""
    lib = new_library(tmp_path)
    make_client(lib)
    deep = lib.root
    for _ in range(600):
        deep = deep / "a"
        deep.mkdir()
    start = open_fds()
    found = lib.find("Acme")
    assert [row["id"] for row in found.results] == ["acme-bakery"]
    assert found.total == 1
    assert open_fds() == start, "the walk leaked a descriptor"


# --- order and bookkeeping equivalence --------------------------------------


def reference_walk(root):
    """A plain recursive depth-first scan by the documented rules.

    Only for comparing order and bookkeeping; the tree built by the test is
    small enough that reading whole candidate files is equivalent to the
    discovery window.
    """
    entries, skipped, oversize = [], [], []

    def visit(directory, relative):
        with os.scandir(directory) as scanner:
            children = sorted(scanner, key=lambda e: e.name)
        for child in children:
            rel = f"{relative}/{child.name}" if relative else child.name
            if child.is_symlink():
                if not child.name.startswith(".") and (child.name.endswith(".md") or child.is_dir()):
                    skipped.append(rel)
            elif child.is_dir(follow_symlinks=False):
                if not child.name.startswith("."):
                    visit(child.path, rel)
            elif child.is_file(follow_symlinks=False) and child.name.endswith(".md"):
                size = child.stat(follow_symlinks=False).st_size
                if size > MAX_RECORD_BYTES:
                    oversize.append((rel, size))
                elif parse(Path(child.path).read_bytes()).is_record:
                    entries.append(rel)

    visit(root, "")
    return entries, skipped, oversize


def test_walk_order_is_depth_first_with_files_and_directories_interleaved(tmp_path):
    lib = new_library(tmp_path)
    write(lib.root, "Acme Bakery/Client.md", record_bytes("client", "acme-bakery", "Acme Bakery"))
    write(lib.root, "Acme Bakery/Projects/Website 2026.md",
          record_bytes("project", "acme-website-2026", "Website 2026", client="acme-bakery"))
    write(lib.root, "Acme Bakery/Projects/notes.txt", "not a record\n")
    # Sorts before "Acme Bakery/..." as a string but after the directory "Acme Bakery".
    write(lib.root, "Acme Bakery.md", record_bytes("client", "acme-notes", "Acme Notes"))
    write(lib.root, "Riverbend Dental/Client.md", record_bytes("client", "riverbend-dental", "Riverbend Dental"))
    write(lib.root, "Riverbend Dental/Oversize.md", b"---\n" + b"x" * (MAX_RECORD_BYTES + 1))
    write(lib.root, "Zed/Deep/Inner.md", record_bytes("client", "zed-inner", "Zed Inner"))
    write(lib.root, ".hidden/Client.md", record_bytes("client", "hidden-client", "Hidden"))
    outside = tmp_path / "outside"
    write(outside, "Client.md", record_bytes("client", "outside-client", "Outside"))
    os.symlink(outside / "Client.md", lib.root / "Riverbend Dental" / "Link.md")

    found = lib.discover()
    expected = [
        "Acme Bakery/Client.md",
        "Acme Bakery/Projects/Website 2026.md",
        "Acme Bakery.md",
        "Riverbend Dental/Client.md",
        "Zed/Deep/Inner.md",
    ]
    assert [e.relpath for e in found.entries] == expected
    assert found.skipped_symlinks == ["Riverbend Dental/Link.md"]
    assert [path for path, _ in found.oversize] == ["Riverbend Dental/Oversize.md"]
    assert found.unreadable == []
    # The same rules, walked recursively in the test, agree with the iterative walk.
    assert reference_walk(lib.root) == (expected, found.skipped_symlinks, found.oversize)


@needs_proc
def test_a_base_exception_mid_walk_releases_every_held_descriptor(tmp_path, monkeypatch):
    lib = new_library(tmp_path)
    make_client(lib)
    (lib.root / "Acme Bakery" / "Projects").mkdir()
    write(lib.root, "Acme Bakery/Projects/Website 2026.md",
          record_bytes("project", "acme-website-2026", "Website 2026", client="acme-bakery"))

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(library_module, "_scan_file", interrupted)
    start = open_fds()
    with pytest.raises(KeyboardInterrupt):
        lib.discover()
    assert open_fds() == start, "an interrupted walk leaked a descriptor"


# --- create refuses an incomplete inventory ---------------------------------


@pytest.mark.skipif(IS_ROOT, reason="root ignores file modes")
def test_create_refuses_when_a_candidate_record_is_unreadable(tmp_path):
    lib = new_library(tmp_path)
    make_client(lib)
    sealed = lib.root / "Acme Bakery" / "Client.md"
    sealed.chmod(0)
    before = top_level(lib.root)
    try:
        assert lib.discover().unreadable == ["Acme Bakery/Client.md"]
        with pytest.raises(EngagementsError) as exc:
            lib.create("client", "Acme-Bakery", profile="studio-dev", today=fixed_clock)
    finally:
        sealed.chmod(0o644)
    assert refused(exc.value), (exc.value.code, exc.value.details)
    assert exc.value.details["path"] == "Acme Bakery/Client.md"
    assert top_level(lib.root) == before, "a refused create wrote something"


@pytest.mark.skipif(IS_ROOT, reason="root ignores file modes")
def test_the_refusal_path_does_not_depend_on_the_cwd(tmp_path, monkeypatch):
    """The unreadable relpath is anchored at the root, not resolved against the cwd."""
    lib = new_library(tmp_path)
    make_client(lib)
    sealed = lib.root / "Acme Bakery" / "Client.md"
    monkeypatch.chdir(lib.root / "Acme Bakery")
    sealed.chmod(0)
    try:
        with pytest.raises(EngagementsError) as exc:
            lib.create("client", "Acme-Bakery", profile="studio-dev", today=fixed_clock)
    finally:
        sealed.chmod(0o644)
    assert refused(exc.value)
    assert exc.value.details["path"] == "Acme Bakery/Client.md"


@pytest.mark.skipif(IS_ROOT, reason="root ignores file modes")
def test_create_refuses_when_a_subtree_is_unreadable(tmp_path):
    lib = new_library(tmp_path)
    make_client(lib)
    sealed = lib.root / "Acme Bakery"
    sealed.chmod(0)
    before = top_level(lib.root)
    try:
        assert lib.discover().unreadable == ["Acme Bakery"]
        with pytest.raises(EngagementsError) as exc:
            lib.create("client", "Riverbend Dental", profile="studio-dev", today=fixed_clock)
    finally:
        sealed.chmod(0o755)
    assert refused(exc.value), (exc.value.code, exc.value.details)
    assert exc.value.details["path"] == "Acme Bakery"
    assert top_level(lib.root) == before, "a refused create wrote something"


@pytest.mark.skipif(IS_ROOT, reason="root ignores file modes")
def test_project_create_reports_an_incomplete_scan_not_a_missing_client(tmp_path):
    lib = new_library(tmp_path)
    make_client(lib)
    sealed = lib.root / "Acme Bakery" / "Client.md"
    sealed.chmod(0)
    try:
        with pytest.raises(EngagementsError) as exc:
            lib.create("project", "Website 2027", client="acme-bakery", profile="studio-dev", today=fixed_clock)
    finally:
        sealed.chmod(0o644)
    assert refused(exc.value), (exc.value.code, exc.value.details)
    assert "Acme-Bakery" not in top_level(lib.root)


def test_a_readable_library_still_refuses_as_similar(tmp_path):
    lib = new_library(tmp_path)
    make_client(lib)
    with pytest.raises(EngagementsError) as exc:
        lib.create("client", "Acme-Bakery", profile="studio-dev", today=fixed_clock)
    assert exc.value.code == "similar_exists"
    assert [c["id"] for c in exc.value.details["candidates"]] == ["acme-bakery"]


def test_intentional_exclusions_do_not_block_create(tmp_path):
    lib = new_library(tmp_path)
    make_client(lib)
    outside = tmp_path / "outside"
    write(outside, "Client.md", record_bytes("client", "outside-client", "Outside"))
    os.symlink(outside / "Client.md", lib.root / "Linked File.md")
    write(lib.root, ".drafts/Client.md", record_bytes("client", "draft-client", "Draft"))
    write(lib.root, "Riverbend Dental/Oversize.md", b"---\n" + b"x" * (MAX_RECORD_BYTES + 1))
    found = lib.discover()
    assert found.unreadable == []
    assert "Linked File.md" in found.skipped_symlinks
    created = lib.create("client", "Riverbend Dental", profile="studio-dev", today=fixed_clock)
    assert created.id == "riverbend-dental"
    assert (lib.root / created.path).is_file()


# --- descriptor exhaustion reaches the same refusal -------------------------


def test_a_level_that_cannot_be_opened_is_unreadable_and_blocks_create(tmp_path, monkeypatch):
    lib = new_library(tmp_path)
    make_client(lib)
    write(lib.root, "Sealed/Client.md", record_bytes("client", "riverbend-dental", "Riverbend Dental"))
    real = _Dir.open_dir

    def exhausted(self, name, writable=None):
        if name == "Sealed":
            raise OSError(errno.EMFILE, "Too many open files")
        return real(self, name) if writable is None else real(self, name, writable=writable)

    monkeypatch.setattr(_Dir, "open_dir", exhausted)
    start = open_fds() if os.path.isdir("/proc/self/fd") else None
    found = lib.discover()  # recorded, not raised
    assert found.unreadable == ["Sealed"]
    assert [e.id for e in found.entries] == ["acme-bakery"]
    if start is not None:
        assert open_fds() == start
    with pytest.raises(EngagementsError) as exc:
        lib.create("client", "Riverbend Dental", profile="studio-dev", today=fixed_clock)
    assert refused(exc.value), (exc.value.code, exc.value.details)
    assert exc.value.details["path"] == "Sealed"
    assert "Riverbend Dental" not in top_level(lib.root)


# --- exclusions that fail their own checks still never block ------
#
# A symlink whose target cannot be checked (a loop, or a target below an
# inaccessible directory) and an oversized file the plugin may not read are still
# intentional exclusions: none can be a record. They used to land in
# ``unreadable``, which the inventory gate then treated as a hole in the inventory.


def _blocked_target(lib, tmp_path):
    vault = tmp_path / "vault"
    (vault / "inner").mkdir(parents=True)
    os.symlink(vault / "inner", lib.root / "Linked")
    vault.chmod(0)
    return vault


def _sealed_oversize(lib, tmp_path):
    big = lib.root / "Oversize.md"
    big.write_bytes(b"---\n" + b"x" * MAX_RECORD_BYTES)
    big.chmod(0)
    return big


def _self_loop(lib, tmp_path):
    os.symlink("Linked", lib.root / "Linked")
    return None


@pytest.mark.skipif(IS_ROOT, reason="root ignores file modes")
@pytest.mark.parametrize(("setup", "bucket", "name"), [
    (_self_loop, "skipped_symlinks", "Linked"),
    (_blocked_target, "skipped_symlinks", "Linked"),
    (_sealed_oversize, "oversize", "Oversize.md"),
])
def test_an_exclusion_that_fails_its_own_check_does_not_block_create(tmp_path, setup, bucket, name):
    lib = new_library(tmp_path)
    make_client(lib)
    sealed = setup(lib, tmp_path)
    try:
        found = lib.discover()
        assert found.unreadable == []
        listed = [item[0] if isinstance(item, tuple) else item for item in getattr(found, bucket)]
        assert name in listed
        created = lib.create("client", "Riverbend Dental", profile="studio-dev", today=fixed_clock)
    finally:
        if sealed is not None:
            sealed.chmod(0o755 if sealed.is_dir() else 0o644)
    assert created.id == "riverbend-dental"


@pytest.mark.skipif(IS_ROOT, reason="root ignores file modes")
def test_a_sealed_small_candidate_is_still_unreadable(tmp_path):
    """Control: only a provably oversized file is excluded without being read."""
    lib = new_library(tmp_path)
    make_client(lib)
    small = lib.root / "Notes.md"
    small.write_bytes(b"---\nkind: client\n---\n")
    small.chmod(0)
    try:
        assert lib.discover().unreadable == ["Notes.md"]
        with pytest.raises(EngagementsError) as exc:
            lib.create("client", "Riverbend Dental", profile="studio-dev", today=fixed_clock)
    finally:
        small.chmod(0o644)
    assert refused(exc.value)
