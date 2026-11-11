"""Discovery holds the root and each child directory open for the whole walk, so a directory
swapped mid-walk is never followed. Fictional data only.

Discovery used to check the root, close it, then walk by pathname. A checked
root or child directory replaced by a symlink before it was scanned, or a
directory replaced before a candidate was opened, made ``find`` return records
from outside the library under in-library paths. Discovery now holds the
opened root and each no-follow child directory for the whole walk and opens
every candidate relative to its held directory (TOOLS.md "Filesystem
boundary").

The swaps model another local writer racing the plugin. Each fires once, at a
deterministic point, on a real filesystem; the original directory is kept (as
a renamed sibling), so reading it through a held descriptor is the expected
outcome and reading the outside library is the failure.
"""

import os

import pytest
from engagements.errors import EngagementsError
from engagements.library import open_library
from writer_support import fixed_clock

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX symlink swaps")


@pytest.fixture
def libraries(tmp_path):
    """An Acme Bakery library and, outside it, a separate Riverbend Dental library."""
    inside, outside = tmp_path / "library", tmp_path / "elsewhere"
    inside.mkdir()
    outside.mkdir()
    lib = open_library(inside, create_default=True)
    created = lib.create("client", "Acme Bakery", profile="studio-dev", today=fixed_clock)
    other = open_library(outside, create_default=True)
    foreign = other.create("client", "Riverbend Dental", profile="studio-dev", today=fixed_clock)
    return lib, inside / os.path.dirname(created.path), outside, outside / os.path.dirname(foreign.path)


def swap_for_link(path, target):
    """Move ``path`` aside (kept) and put a symlink to ``target`` in its place."""
    os.rename(path, path.with_name(path.name + "-kept"))
    os.symlink(target, path)


def ids(discovery):
    return sorted(e.id for e in discovery.entries if e.id)


def hook(monkeypatch, name, when, action):
    """Run ``action`` once, just before the ``when(args)``-th real ``os.<name>`` call proceeds."""
    real = getattr(os, name)
    fired = []

    def wrapped(*args, **kwargs):
        if not fired and when(args, kwargs):
            fired.append(True)
            action()
        return real(*args, **kwargs)

    monkeypatch.setattr(os, name, wrapped)
    return fired


def nth(n):
    calls = []

    def when(args, kwargs):
        calls.append(True)
        return len(calls) == n

    return when


def test_child_directory_swapped_before_its_scan_is_never_followed(libraries, monkeypatch):
    lib, acme_dir, _, riverbend_dir = libraries
    # Scan 1 is the root; scan 2 is the only record directory, Acme Bakery.
    fired = hook(monkeypatch, "scandir", nth(2), lambda: swap_for_link(acme_dir, riverbend_dir))
    found = lib.discover()
    assert fired
    assert "riverbend-dental" not in ids(found)
    assert lib.find("Riverbend Dental").total == 0


def test_root_swapped_after_verification_is_never_followed(libraries, monkeypatch):
    lib, _, outside, _ = libraries
    fired = hook(monkeypatch, "scandir", nth(1), lambda: swap_for_link(lib.root, outside))
    try:
        found = lib.discover()
    except EngagementsError as exc:  # refusing the replaced root is also acceptable
        assert (exc.code, exc.details.get("reason")) == ("io_error", "root_changed")
    else:
        assert "riverbend-dental" not in ids(found)
    assert fired


def test_directory_swapped_before_a_candidate_is_opened_is_never_followed(libraries, monkeypatch):
    lib, acme_dir, _, riverbend_dir = libraries

    def opening_client(args, kwargs):
        return bool(args) and os.path.basename(os.fsdecode(args[0])) == "Client.md"

    fired = hook(monkeypatch, "open", opening_client, lambda: swap_for_link(acme_dir, riverbend_dir))
    found = lib.discover()
    assert fired
    assert "riverbend-dental" not in ids(found)


def test_a_stable_library_is_still_fully_discovered(libraries):
    lib, *_ = libraries
    found = lib.discover()
    assert ids(found) == ["acme-bakery"]
    assert lib.find("Acme").total == 1


def test_a_symlinked_directory_is_still_skipped_and_reported(libraries):
    lib, _, _, riverbend_dir = libraries
    os.symlink(riverbend_dir, lib.root / "Linked Client")
    found = lib.discover()
    assert ids(found) == ["acme-bakery"]
    assert "Linked Client" in found.skipped_symlinks


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="needs /proc/self/fd")
def test_discovery_leaks_no_descriptors(libraries):
    lib, *_ = libraries
    (lib.root / "Acme Bakery" / "Projects").mkdir(exist_ok=True)
    start = len(os.listdir("/proc/self/fd"))
    for _ in range(3):
        lib.discover()
    assert len(os.listdir("/proc/self/fd")) == start


# ---- byte-string roots ----------------------------------------------------------


@pytest.mark.parametrize("create_default", [True, False])
def test_a_bytes_root_is_invalid_op(tmp_path, create_default):
    with pytest.raises(EngagementsError) as exc:
        open_library(os.fsencode(tmp_path / "not-created"), create_default=create_default)
    assert (exc.value.code, exc.value.details) == ("invalid_op", {"field": "library_root", "reason": "invalid_value"})
