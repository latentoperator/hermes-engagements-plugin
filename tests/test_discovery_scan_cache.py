"""What a second walk may reuse, and what it must read again.

The library keeps an in-process cache of what each file said, keyed by the root's
identity and the file's relative path and validated by the file's own identity
(device, inode or file index, size and mtime, and on POSIX the change time that
makes a preserved-timestamp edit visible). The cache is used only where that
identity can exist: a platform whose ``os.stat`` exposes no change time reads
every file on every walk, and these tests select that branch to prove it. Every
case here changes the library the way an editor, a writer or a user does and
asserts that the next discovery says what the files say now. The one measurement
is opens, never seconds.
"""

import json
import os
import sys
import threading
import time

import pytest
from conftest import record_bytes, write
from engagements import library as library_module
from engagements.library import Library, _ScanCache, open_library
from tools_support import call, make_session

# Tests that observe a cache hit only mean something while the cache is in use.
# The suite is also run with it switched off (``ENGAGEMENTS_SCAN_CACHE=0``), and a
# platform without a change time never uses it at all: its stat facts cannot tell a
# preserved-timestamp edit from the read they would answer with.
CACHE_ONLY = pytest.mark.skipif(
    not library_module._SCAN_CACHE_ENABLED or not library_module._cache_platform_supported(),
    reason="cached reads need the cache switched on and a stat that exposes a change time",
)


class _NoChangeTimeOS:
    """The real ``os`` module under the platform name whose stat has no change time.

    Selecting that branch is how the platform limit is exercised on any host: the
    cache must not be used, so every walk reads every candidate.
    """

    name = "nt"
    _real = os

    def __getattr__(self, attribute):
        return getattr(self._real, attribute)


@pytest.fixture(autouse=True)
def isolated_cache(monkeypatch):
    """Every test starts with an empty process cache of its own."""
    monkeypatch.setattr(library_module, "_SCAN_CACHE", _ScanCache())


@pytest.fixture
def lib(tmp_path):
    return open_library(tmp_path / "library", create_default=True)


def age(root):
    """Move every file's mtime an hour into the past, as an established library's would be.

    A file written a moment ago is inside the racy window (two seconds either side
    of the read that caches it) and is read again by design, so a test that wants
    to observe a cache hit first has to age the files. This changes ctime as well,
    before anything has been cached.
    """
    old = time.time_ns() - 3600 * 1000 * 1000 * 1000
    for path in root.rglob("*"):
        if path.is_file():
            os.utime(path, ns=(old, old))


def count_reads(function):
    """Run ``function`` and report how many candidates it read (its file opens)."""
    reads = []
    original = library_module._scan_file

    def counting(*args, **kwargs):
        reads.append(args[2])
        return original(*args, **kwargs)

    library_module._scan_file = counting
    try:
        value = function()
    finally:
        library_module._scan_file = original
    return value, reads


def entries(discovery):
    return {entry.relpath: entry for entry in discovery.entries}


@CACHE_ONLY
def test_a_second_walk_of_an_unchanged_library_opens_no_file(lib, tmp_path):
    write(lib.root, "Acme Bakery/Client.md", record_bytes("client", "acme-bakery", "Acme Bakery"))
    write(lib.root, "Notes/Scratch.md", b"no frontmatter here\n")
    age(lib.root)
    first, first_reads = count_reads(lib.discover)
    second, second_reads = count_reads(lib.discover)
    third, third_reads = count_reads(lib.discover)
    assert sorted(first_reads) == ["Acme Bakery/Client.md", "Notes/Scratch.md"]
    assert second_reads == third_reads == [], "an unchanged library was read again"
    assert [e.relpath for e in first.entries] == [e.relpath for e in second.entries] \
        == [e.relpath for e in third.entries] == ["Acme Bakery/Client.md"]
    assert second.listed == third.listed == first.listed >= 3, "the walk itself still happens"


def test_an_edit_in_place_is_read_again(lib):
    path = write(lib.root, "Acme Bakery/Client.md", record_bytes("client", "acme-bakery", "Acme Bakery"))
    age(lib.root)
    assert entries(lib.discover())["Acme Bakery/Client.md"].name == "Acme Bakery"
    path.write_bytes(record_bytes("client", "acme-bakery", "Acme Bakery Renamed"))
    assert entries(lib.discover())["Acme Bakery/Client.md"].name == "Acme Bakery Renamed"


def test_an_atomic_replace_is_read_again(lib):
    path = write(lib.root, "Acme Bakery/Client.md", record_bytes("client", "acme-bakery", "Acme Bakery"))
    age(lib.root)
    assert entries(lib.discover())["Acme Bakery/Client.md"].id == "acme-bakery"
    replacement = path.with_name("Client.md.new")
    replacement.write_bytes(record_bytes("client", "acme-bakery", "Acme Bakery Replaced"))
    os.replace(replacement, path)
    assert entries(lib.discover())["Acme Bakery/Client.md"].name == "Acme Bakery Replaced"


def test_a_same_size_edit_in_the_same_second_is_read_again(lib):
    """Two different contents, same length, written inside one timestamp tick."""
    first = record_bytes("client", "acme-bakery", "Acme Bakery")
    second = record_bytes("client", "acme-bakery", "Acme Bakerz")
    assert first != second and len(first) == len(second)
    path = write(lib.root, "Acme Bakery/Client.md", first)
    assert entries(lib.discover())["Acme Bakery/Client.md"].name == "Acme Bakery"
    path.write_bytes(second)
    found = entries(lib.discover())["Acme Bakery/Client.md"]
    assert found.name == "Acme Bakerz" and found.rev == library_module.rev(second)


def test_a_deleted_record_is_gone_and_its_entry_is_evicted(lib):
    path = write(lib.root, "Acme Bakery/Client.md", record_bytes("client", "acme-bakery", "Acme Bakery"))
    age(lib.root)
    assert lib.discover().entries
    path.unlink()
    assert lib.discover().entries == []
    # The path is no longer cached, so a new record there is read rather than
    # answered from the deleted one's entry.
    write(lib.root, "Acme Bakery/Client.md", record_bytes("client", "other-client", "Other Client"))
    assert [e.id for e in lib.discover().entries] == ["other-client"]


def test_a_renamed_record_is_read_under_its_new_path(lib):
    path = write(lib.root, "Acme Bakery/Client.md", record_bytes("client", "acme-bakery", "Acme Bakery"))
    age(lib.root)
    assert list(entries(lib.discover())) == ["Acme Bakery/Client.md"]
    path.rename(path.with_name("Client Copy.md"))
    found = entries(lib.discover())
    assert list(found) == ["Acme Bakery/Client Copy.md"]
    assert found["Acme Bakery/Client Copy.md"].id == "acme-bakery"


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0 or sys.platform == "win32",
                    reason="permission bits do not bind as root or on Windows")
def test_a_file_that_became_unreadable_is_not_served_from_its_cached_read(lib):
    path = write(lib.root, "Acme Bakery/Client.md", record_bytes("client", "acme-bakery", "Acme Bakery"))
    age(lib.root)
    assert [e.id for e in lib.discover().entries] == ["acme-bakery"]
    path.chmod(0)
    try:
        discovery = lib.discover()
    finally:
        path.chmod(0o644)
    assert discovery.entries == []
    assert discovery.unreadable == ["Acme Bakery/Client.md"]
    assert [e.id for e in lib.discover().entries] == ["acme-bakery"]


def test_a_file_that_became_a_record_is_read_again(lib):
    path = write(lib.root, "Notes/Scratch.md", b"just a note\n")
    age(lib.root)
    assert lib.discover().entries == []
    path.write_bytes(record_bytes("client", "acme-bakery", "Acme Bakery"))
    assert [e.id for e in lib.discover().entries] == ["acme-bakery"]


def test_a_root_swapped_for_another_directory_is_a_different_cache(lib, tmp_path):
    write(lib.root, "Acme Bakery/Client.md", record_bytes("client", "acme-bakery", "Acme Bakery"))
    age(lib.root)
    assert [e.id for e in lib.discover().entries] == ["acme-bakery"]
    lib.root.rename(tmp_path / "library-old")
    fresh = open_library(tmp_path / "library", create_default=True)
    write(fresh.root, "Acme Bakery/Client.md", record_bytes("client", "other-client", "Other Client"))
    assert [e.id for e in fresh.discover().entries] == ["other-client"]


@CACHE_ONLY
def test_two_library_objects_on_one_root_share_what_was_read(lib, tmp_path):
    write(lib.root, "Acme Bakery/Client.md", record_bytes("client", "acme-bakery", "Acme Bakery"))
    age(lib.root)
    first, first_reads = count_reads(lib.discover)
    second_library = Library(lib.root, lib.library_id, False, lib.root_id)
    second, second_reads = count_reads(second_library.discover)
    assert first_reads and second_reads == []
    assert [e.relpath for e in second.entries] == [e.relpath for e in first.entries]
    # A walk that served everything from the cache must leave the cache as full as it found it.
    _, third_reads = count_reads(lib.discover)
    assert third_reads == [], "a walk of cache hits emptied the cache for the next walk"


def test_two_threads_discovering_one_root_agree(lib):
    for index in range(20):
        write(lib.root, f"Client {index}/Client.md",
              record_bytes("client", f"client-{index}", f"Client {index}"))
    age(lib.root)
    results, failures = [], []

    def walk():
        try:
            results.append(sorted(e.id for e in lib.discover().entries))
        except BaseException as exc:  # noqa: BLE001 - a thread's failure is the test's failure
            failures.append(exc)

    threads = [threading.Thread(target=walk) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert failures == []
    assert results == [sorted(f"client-{index}" for index in range(20))] * 4


def test_duplicate_ids_are_recomputed_from_every_entry_on_every_walk(lib):
    write(lib.root, "A/Client.md", record_bytes("client", "acme-bakery", "Acme Bakery"))
    write(lib.root, "B/Client.md", record_bytes("client", "acme-bakery", "Acme Bakery Two"))
    age(lib.root)
    assert all("duplicate_id" in e.defects for e in lib.discover().entries)
    (lib.root / "B/Client.md").unlink()
    assert [e.defects for e in lib.discover().entries] == [frozenset()]


@CACHE_ONLY
def test_the_walk_with_no_record_is_remembered_too(lib):
    write(lib.root, "Notes/Scratch.md", b"just a note\n")
    age(lib.root)
    count_reads(lib.discover)
    _, reads = count_reads(lib.discover)
    assert reads == []


@CACHE_ONLY
def test_a_walk_over_the_path_bound_keeps_no_cache_and_still_answers(tmp_path, monkeypatch):
    monkeypatch.setattr(library_module, "_SCAN_CACHE", _ScanCache(paths=1))
    lib = open_library(tmp_path / "library", create_default=True)
    write(lib.root, "A/Client.md", record_bytes("client", "client-a", "Client A"))
    write(lib.root, "B/Client.md", record_bytes("client", "client-b", "Client B"))
    age(lib.root)
    first, first_reads = count_reads(lib.discover)
    assert sorted(first_reads) == ["A/Client.md", "B/Client.md"]
    assert sorted(e.id for e in first.entries) == ["client-a", "client-b"]
    _, second_reads = count_reads(lib.discover)
    assert sorted(second_reads) == ["A/Client.md", "B/Client.md"], "a root over the path bound kept a cache"


@CACHE_ONLY
def test_only_the_root_bound_is_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(library_module, "_SCAN_CACHE", _ScanCache(roots=1))
    one = open_library(tmp_path / "one", create_default=True)
    two = open_library(tmp_path / "two", create_default=True)
    write(one.root, "A/Client.md", record_bytes("client", "client-a", "Client A"))
    write(two.root, "B/Client.md", record_bytes("client", "client-b", "Client B"))
    age(one.root)
    age(two.root)
    count_reads(one.discover)
    count_reads(two.discover)
    _, reads = count_reads(one.discover)
    assert reads == ["A/Client.md"], "the oldest root's cache was kept past the root bound"


@CACHE_ONLY
def test_the_cache_can_be_switched_off_and_both_paths_agree(lib, monkeypatch):
    write(lib.root, "Acme Bakery/Client.md", record_bytes("client", "acme-bakery", "Acme Bakery"))
    write(lib.root, "Notes/Scratch.md", b"just a note\n")
    write(lib.root, "Broken/Client.md", b"---\nengagements: client\nid: [unclosed\n---\n")
    age(lib.root)
    cached = lib.discover()
    _, reads = count_reads(lib.discover)
    assert reads == []
    monkeypatch.setattr(library_module, "_SCAN_CACHE_ENABLED", False)
    fresh, fresh_reads = count_reads(lib.discover)
    assert sorted(fresh_reads) == ["Acme Bakery/Client.md", "Broken/Client.md", "Notes/Scratch.md"]
    assert [(e.relpath, e.id, e.defects) for e in fresh.entries] == \
           [(e.relpath, e.id, e.defects) for e in cached.entries]
    assert fresh.unreadable == cached.unreadable
    assert fresh.listed == cached.listed


@CACHE_ONLY
def test_locations_come_from_the_same_read_that_found_the_project(lib):
    """Automatic binding needs no second read of a project's bytes."""
    body = (b"---\nengagements: project\nid: acme-website\nname: Website\nclient: acme-bakery\n"
            b"status: active\nlocations: [/work/shared]\n---\n\n## Summary\n\nFictional.\n")
    write(lib.root, "Acme Bakery/Projects/Website.md", body)
    age(lib.root)
    discovery = lib.discover()
    assert discovery.project_locations["Acme Bakery/Projects/Website.md"] == ("/work/shared",)
    calls = []
    original = library_module._scan_file

    def counting(*args, **kwargs):
        calls.append(args[2])
        return original(*args, **kwargs)

    library_module._scan_file = counting
    try:
        again = lib.discover()
    finally:
        library_module._scan_file = original
    assert calls == []
    assert again.project_locations == discovery.project_locations


def test_a_project_whose_locations_are_defective_reports_them_as_unknown(lib):
    body = (b"---\nengagements: project\nid: acme-website\nname: Website\nclient: acme-bakery\n"
            b"status: active\nlocations: [one]\nlocations: [two]\n---\n\n## Summary\n\nFictional.\n")
    write(lib.root, "Acme Bakery/Projects/Website.md", body)
    assert lib.discover().project_locations["Acme Bakery/Projects/Website.md"] is None


def test_the_scan_cache_is_not_used_where_the_platform_exposes_no_change_time(lib, monkeypatch):
    """The platform branch has no safe identity, so every walk reads every candidate."""
    write(lib.root, "Acme Bakery/Client.md", record_bytes("client", "acme-bakery", "Acme Bakery"))
    age(lib.root)
    monkeypatch.setattr(library_module, "_SCAN_CACHE", _ScanCache())
    monkeypatch.setattr(library_module, "os", _NoChangeTimeOS())
    first, first_reads = count_reads(lib.discover)
    second, second_reads = count_reads(lib.discover)
    assert first_reads == ["Acme Bakery/Client.md"]
    assert second_reads == ["Acme Bakery/Client.md"], \
        "a platform whose stat exposes no change time answered from the cache"
    assert entries(first) == entries(second)


def test_a_preserved_timestamp_edit_is_read_again_where_no_change_time_is_exposed(lib, monkeypatch):
    """Same length, same nanosecond mtime, new bytes: the cached read must not be served.

    On a platform whose stat exposes no change time every fact the identity holds can
    be restored after an in-place edit, so the walk reads the file again and both the
    name and the automatic binding come from the bytes that are on disk now.
    """
    work = lib.root.parent / "work"
    work.mkdir()
    elsewhere = lib.root.parent / "else"
    elsewhere.mkdir()
    write(lib.root, "Acme Bakery/Client.md", record_bytes("client", "acme-bakery", "Acme Bakery"))
    def project_text(name, location):
        return ("---\nengagements: project\nid: acme-website\nname: " + name + "\nclient: acme-bakery\n"
                "status: active\nlocations: [" + json.dumps(str(location)) + "]\nupdated: 2026-11-05\n"
                "---\n\n## Summary\n\nFictional.\n")

    path = write(lib.root, "Acme Bakery/Projects/Website.md", project_text("Website", work))
    age(lib.root)
    monkeypatch.setattr(library_module, "_SCAN_CACHE", _ScanCache())
    monkeypatch.setattr(library_module, "os", _NoChangeTimeOS())
    lib.discover()  # warm the cache through the selected platform branch
    before = path.stat()
    # A new name and a new location in the same number of bytes, so every fact a
    # platform without a change time can see is the one the cached read recorded.
    renamed = project_text("Websitz", elsewhere).encode()
    assert len(renamed) == before.st_size and renamed != path.read_bytes()
    path.write_bytes(renamed)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    restored = path.stat()
    assert restored.st_size == before.st_size
    # A filesystem that will not hand back the exact nanosecond mtime only makes this
    # attack harder to stage; what the assertions below hold is that a walk reads bytes
    # it cannot tell apart from the cached ones, whatever the stat says.

    def observe():
        session, _store = make_session(lib.root, cwd=str(work))
        return (sorted(entry.name for entry in lib.discover().entries),
                call("engagements_bind", {"mode": "auto"}, session)["data"]["state"])

    warm = observe()
    monkeypatch.setattr(library_module, "_SCAN_CACHE_ENABLED", False)
    cold = observe()
    assert warm == cold
    assert warm == (["Acme Bakery", "Websitz"], "unbound")
