"""The read-only fallback keeps root continuity and excludes symlinked record components when
O_NOFOLLOW is absent (TOOLS.md "Filesystem boundary"). Fictional data only.

The read-only fallback accepted a different ordinary directory at the opened
root's pathname, and followed final record symlinks when O_NOFOLLOW was absent.
TOOLS.md "Filesystem boundary" requires root continuity and excludes symlinked
record components even when writes are unsupported. These are static changes
present before the call, not the accepted path-based discovery race.
"""

import os

import pytest
from conftest import CAN_SYMLINK
from engagements import fsutil, writer
from engagements import library as library_module
from engagements.errors import EngagementsError
from engagements.fsutil import rev
from engagements.library import open_library

ACME = b"---\nengagements: client\nid: acme-bakery\nname: Acme Bakery\n---\n"
RIVERBEND = b"---\nengagements: client\nid: riverbend-dental\nname: Riverbend Dental\n---\n"


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "library"
    meta = root / ".engagements"
    meta.mkdir(parents=True)
    (meta / "library.yaml").write_text(
        "format: 1\nlibrary_id: 12345678-1234-5678-1234-567812345678\n", encoding="utf-8")
    (root / "Client.md").write_bytes(ACME)
    outside = tmp_path / "Riverbend.md"
    outside.write_bytes(RIVERBEND)
    return open_library(root, create_default=False), outside


MODES = ["native", "no-dir_fd", "no-nofollow", "neither"]


def simulate(monkeypatch, mode):
    """Remove the actual read-open flags too, not just the write capability."""
    if mode != "native":
        monkeypatch.setattr(fsutil, "_HANDLES", False)  # no Windows handle backend on the simulated platform
    if mode in ("no-dir_fd", "neither"):
        monkeypatch.setattr(fsutil, "_RELATIVE", False)
    if mode in ("no-nofollow", "neither"):
        monkeypatch.setattr(fsutil, "_NOFOLLOW", 0)
        monkeypatch.setattr(library_module, "_NOFOLLOW", 0)
        monkeypatch.setattr(writer, "_READ_FLAGS", writer._READ_FLAGS & ~getattr(os, "O_NOFOLLOW", 0))
        # Discovery computes flags at call time; simulate that platform as well.
        monkeypatch.setattr(os, "O_NOFOLLOW", 0, raising=False)


def assert_error(call, code, reason):
    with pytest.raises(EngagementsError) as exc:
        call()
    assert (exc.value.code, exc.value.details.get("reason")) == (code, reason)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("operation", ["discover", "find", "read"])
def test_replaced_ordinary_root_is_refused(library, monkeypatch, mode, operation):
    lib, _ = library
    lib.root.rename(lib.root.with_name("library-kept"))
    lib.root.mkdir()
    (lib.root / "Client.md").write_bytes(RIVERBEND)
    simulate(monkeypatch, mode)
    calls = {
        "discover": lib.discover,
        "find": lambda: lib.find("Riverbend Dental"),
        "read": lambda: writer.read_record(lib, "Client.md"),
    }
    assert_error(calls[operation], "io_error", "root_changed")


@pytest.mark.skipif(not CAN_SYMLINK, reason="this host cannot create symlinks")
@pytest.mark.parametrize("mode", MODES)
def test_final_symlink_is_excluded_and_reported(library, monkeypatch, mode):
    lib, outside = library
    (lib.root / "Linked.md").symlink_to(outside)
    simulate(monkeypatch, mode)
    assert_error(lambda: writer.read_record(lib, "Linked.md"), "not_found", "excluded")
    found = lib.discover()
    assert [entry.id for entry in found.entries] == ["acme-bakery"]
    assert "Linked.md" in found.skipped_symlinks
    assert lib.find("Riverbend Dental").total == 0


@pytest.mark.parametrize("mode", MODES)
def test_normal_reads_still_work(library, monkeypatch, mode):
    lib, _ = library
    simulate(monkeypatch, mode)
    # Opening an existing library also reads library.yaml in fallback mode.
    reopened = open_library(lib.root, create_default=False)
    assert reopened.root_id == lib.root_id
    assert writer.read_record(reopened, "Client.md") == (ACME, rev(ACME))
    assert [entry.id for entry in lib.discover().entries] == ["acme-bakery"]
    assert lib.find("Acme Bakery").total == 1


@pytest.mark.skipif(not CAN_SYMLINK, reason="this host cannot create symlinks")
@pytest.mark.parametrize("mode", MODES)
def test_symlinked_record_directory_is_excluded(library, monkeypatch, mode):
    lib, outside = library
    (lib.root / "Linked").symlink_to(outside.parent, target_is_directory=True)
    simulate(monkeypatch, mode)
    # The writer rejects an outside parent independently of the final file.
    assert_error(lambda: writer.read_record(lib, "Linked/Riverbend.md"), "outside_root", None)
    found = lib.discover()
    assert [entry.id for entry in found.entries] == ["acme-bakery"]
    assert "Linked" in found.skipped_symlinks


@pytest.mark.skipif(not CAN_SYMLINK, reason="this host cannot create symlinks")
@pytest.mark.parametrize("mode", ["no-nofollow", "neither"])
def test_candidate_opener_itself_rejects_a_static_symlink(library, monkeypatch, mode):
    lib, outside = library
    (lib.root / "Linked.md").symlink_to(outside)
    simulate(monkeypatch, mode)
    discovery = library_module.Discovery()
    # Discovery normally skips static links before reaching _scan_file; verify
    # its file opener also enforces the boundary, independently of traversal.
    with lib.open_root() as top:
        library_module._scan_file(top, "Linked.md", "Linked.md", discovery)
    assert discovery.entries == []
    assert discovery.skipped_symlinks == ["Linked.md"]


@pytest.mark.skipif(not CAN_SYMLINK, reason="this host cannot create symlinks")
@pytest.mark.parametrize("mode", ["no-nofollow", "neither"])
def test_snapshot_symlink_is_not_loaded(library, monkeypatch, mode):
    lib, outside = library
    snapshots = lib.root / ".engagements" / "snapshots"
    snapshots.mkdir()
    revision = rev(RIVERBEND)
    (snapshots / f"{revision}.md").symlink_to(outside)
    simulate(monkeypatch, mode)
    assert writer.load_snapshot(lib, revision) is None


@pytest.mark.skipif(not CAN_SYMLINK, reason="this host cannot create symlinks")
@pytest.mark.parametrize("mode", ["no-nofollow", "neither"])
def test_library_yaml_symlink_is_not_read(library, monkeypatch, mode):
    lib, _ = library
    path = lib.root / ".engagements" / "library.yaml"
    outside = lib.root.parent / "library.yaml"
    path.rename(outside)
    path.symlink_to(outside)
    simulate(monkeypatch, mode)
    assert_error(lambda: open_library(lib.root, create_default=False), "io_error", "symlink")


@pytest.mark.parametrize("mode", ["no-nofollow", "neither"])
def test_changed_final_identity_is_refused_before_reading(library, monkeypatch, mode):
    lib, _ = library
    real_open = os.open
    real_close = os.close
    opened, closed = [], []
    simulate(monkeypatch, mode)

    def swap_then_open(path, flags, *args, **kwargs):
        if os.path.basename(path) == "Client.md":
            (lib.root / "Client.md").rename(lib.root / "Client-kept.md")
            (lib.root / "Client.md").write_bytes(RIVERBEND)
            fd = real_open(path, flags, *args, **kwargs)
            opened.append(fd)
            return fd
        return real_open(path, flags, *args, **kwargs)

    def close(fd):
        closed.append(fd)
        return real_close(fd)

    monkeypatch.setattr(os, "open", swap_then_open)
    monkeypatch.setattr(os, "close", close)
    assert_error(lambda: writer.read_record(lib, "Client.md"), "not_found", "excluded")
    assert opened and all(fd in closed for fd in opened)


@pytest.mark.skipif(not CAN_SYMLINK, reason="this host cannot create symlinks")
@pytest.mark.parametrize("identity", [False, True])
def test_fallback_root_opener_rejects_a_static_symlink(library, monkeypatch, identity):
    lib, _ = library
    kept = lib.root.with_name("library-kept")
    lib.root.rename(kept)
    lib.root.symlink_to(kept, target_is_directory=True)
    monkeypatch.setattr(fsutil, "_HANDLES", False)
    monkeypatch.setattr(fsutil, "_RELATIVE", False)
    with pytest.raises(fsutil._NotRealDirectory) as exc:
        fsutil._Dir.open_root(lib.root, lib.root_id if identity else None)
    assert exc.value.reason == ("root_changed" if identity else "not_a_directory")
