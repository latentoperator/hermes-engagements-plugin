"""Platforms without ``dir_fd`` or ``O_NOFOLLOW``: every write refuses, reads fall back to
paths, and descriptor-relative errors keep their parent directory. Fictional data only.

The simulations patch the capability flags on Linux. They are capability simulations, not
a native Windows run. Tests marked as controls pass in the ordinary case as well.
"""

import os
import stat
import sys

import pytest
from conftest import record_bytes, write
from engagements import fsutil
from engagements import library as library_module
from engagements.errors import EngagementsError
from engagements.library import open_library
from test_library_open_and_scan_safety import everything, fails, make


@pytest.fixture
def lib(opened_lib):
    return opened_lib

native_writes = pytest.mark.skipif(not library_module._safe_writes(), reason="needs native dir_fd and O_NOFOLLOW")


@pytest.fixture
def outside(tmp_path):
    path = tmp_path / "outside"
    path.mkdir()
    return path


def _records(path):
    return [p for p in everything(path) if not p.startswith(".engagements")]


def _open_fds():
    return set(os.listdir("/proc/self/fd")) if os.path.isdir("/proc/self/fd") else None


def _assert_unsupported(err):
    assert err.code == "io_error", (err.code, err.details)
    assert err.details["reason"] == "unsupported_platform"
    assert not os.path.isabs(err.details["path"])


CAPABILITY_GAPS = [
    pytest.param({"_RELATIVE": False}, id="no-dir_fd"),
    pytest.param({"_RELATIVE": False, "_NOFOLLOW": 0}, id="no-dir_fd-no-nofollow"),
    pytest.param({"_NOFOLLOW": 0}, id="no-nofollow"),
]


def _without(monkeypatch, gaps):
    # The capability flags live in fsutil, shared by the library and the writer
    # since then; library keeps its own imported copy of _NOFOLLOW for flags.
    # A native Windows run has the handle backend instead; the simulated platform has neither.
    monkeypatch.setattr(fsutil, "_HANDLES", False)
    for name, value in gaps.items():
        monkeypatch.setattr(fsutil, name, value)
        if hasattr(library_module, name):
            monkeypatch.setattr(library_module, name, value)


# --- no safe primitives means no writes, not path-based writes ------------------


@pytest.mark.parametrize("gaps", CAPABILITY_GAPS)
def test_client_create_without_safe_primitives_cannot_follow_a_swapped_directory(
        lib, library_root, outside, monkeypatch, gaps):
    # Swap the validated new client directory for an outside
    # link immediately before publication. Refusing before mkdir means there is
    # never a directory to swap, and nothing lands outside.
    real_publish = library_module._publish_exclusive
    swapped = []

    def publish(directory, name, data):
        victim = library_root / "Fallback Swap"
        if not swapped and victim.is_dir():
            os.rmdir(victim)
            os.symlink(outside, victim)
            swapped.append(victim)
        return real_publish(directory, name, data)

    monkeypatch.setattr(library_module, "_publish_exclusive", publish)
    _without(monkeypatch, gaps)
    before = everything(library_root)
    _assert_unsupported(fails(lib, "io_error", name="Fallback Swap"))
    assert not swapped
    assert everything(outside) == []
    assert everything(library_root) == before


@pytest.mark.parametrize("gaps", CAPABILITY_GAPS)
def test_dangling_lock_symlink_without_safe_primitives_creates_nothing_outside(
        lib, library_root, outside, monkeypatch, gaps):
    locks = library_root / ".engagements" / "locks"
    locks.mkdir(parents=True, exist_ok=True)
    (locks / "_library.lock").symlink_to(outside / "planted.lock")
    _without(monkeypatch, gaps)
    _assert_unsupported(fails(lib, "io_error", name="Foo Co"))
    assert everything(outside) == []
    assert not (library_root / "Foo Co").exists()


@pytest.mark.parametrize("gaps", CAPABILITY_GAPS)
def test_project_create_without_safe_primitives_writes_nothing(lib, library_root, monkeypatch, gaps):
    _without(monkeypatch, gaps)
    before = everything(library_root)
    _assert_unsupported(fails(lib, "io_error", kind="project", name="Menu Refresh", client="acme-bakery"))
    assert everything(library_root) == before


@pytest.mark.parametrize("gaps", CAPABILITY_GAPS)
def test_validation_errors_still_precede_the_platform_refusal(lib, monkeypatch, gaps):
    _without(monkeypatch, gaps)
    err = fails(lib, "invalid_op", kind="nonsense")
    assert err.details["field"] == "kind"


@pytest.mark.parametrize("gaps", CAPABILITY_GAPS)
def test_first_use_without_safe_primitives_creates_nothing(tmp_path, monkeypatch, gaps):
    _without(monkeypatch, gaps)
    fresh = tmp_path / "fresh"
    with pytest.raises(EngagementsError) as e:
        open_library(fresh, create_default=True)
    _assert_unsupported(e.value)
    assert not fresh.exists()

    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(EngagementsError) as e:
        open_library(empty, create_default=True)
    _assert_unsupported(e.value)
    assert everything(empty) == []


@pytest.mark.parametrize("gaps", CAPABILITY_GAPS)
def test_invalid_library_yaml_recheck_never_opens_the_lock_without_safe_primitives(
        library_root, outside, monkeypatch, gaps):
    meta = library_root / ".engagements"
    (meta / "library.yaml").write_bytes(b"")
    (meta / "locks").mkdir(exist_ok=True)
    (meta / "locks" / "_library.lock").symlink_to(outside / "planted.lock")
    _without(monkeypatch, gaps)
    with pytest.raises(EngagementsError) as e:
        open_library(library_root, create_default=True)
    _assert_unsupported(e.value)
    assert everything(outside) == []
    assert (meta / "library.yaml").read_bytes() == b""


@pytest.mark.parametrize("gaps", CAPABILITY_GAPS)
def test_reads_still_work_without_safe_primitives(lib, library_root, monkeypatch, gaps):
    # Control: only writes fail closed; opening an existing library and searching it do not.
    library_id = lib.library_id
    _without(monkeypatch, gaps)
    reopened = open_library(library_root, create_default=False)
    assert reopened.library_id == library_id
    assert reopened.resolve("acme-bakery").kind == "client"
    assert reopened.find("acme").total >= 1


@pytest.mark.parametrize("gaps", CAPABILITY_GAPS)
def test_every_directory_mutation_is_refused_without_safe_primitives(library_root, outside, monkeypatch, gaps):
    # The primitive layer refuses too, so a future caller cannot reach a path-based write.
    _without(monkeypatch, gaps)
    top = library_module._Dir.open_root(library_root)
    with top:
        attempts = [
            lambda: top.mkdir("New"),
            lambda: top.rmdir("Acme Bakery"),
            lambda: top.open("x.md", os.O_WRONLY | os.O_CREAT | os.O_EXCL),
            lambda: top.open("x.md", os.O_RDWR),
            lambda: top.link("Acme Bakery", "y"),
            lambda: top.unlink("x.md"),
        ]
        for attempt in attempts:
            with pytest.raises(fsutil._UnsupportedPlatform):
                attempt()
        with pytest.raises(EngagementsError) as e, library_module._held_lock(library_root, top, "planted.lock", 0.1):
            pass
        _assert_unsupported(e.value)
    assert not (library_root / "New").exists()
    assert not (library_root / "x.md").exists()
    assert not (library_root / "planted.lock").exists()
    assert (library_root / "Acme Bakery").is_dir()


@native_writes
def test_native_platform_still_creates(lib, library_root):
    # Control: the safe primitives exist on this platform, so writes proceed.
    assert library_module._safe_writes()
    result = make(lib, "client", "Foo Co")
    assert (library_root / result.path).is_file()


# --- a new client directory removed after mkdir is a domain error -------------


@native_writes
@pytest.mark.skipif(sys.platform == "win32", reason="simulates a POSIX directory swap with os.mkdir/os.symlink hooks; the Windows threat tests are in test_windows_native.py")
def test_client_directory_removed_after_mkdir_is_io_error(lib, library_root, monkeypatch):
    real_mkdir = os.mkdir
    removed = []

    def mkdir(path, mode=0o777, *, dir_fd=None):
        if dir_fd is None:
            real_mkdir(path, mode)
        else:
            real_mkdir(path, mode, dir_fd=dir_fd)
        if not removed and os.path.basename(os.fspath(path)) == "Removed Client":
            os.rmdir(library_root / "Removed Client")
            removed.append(True)

    monkeypatch.setattr(os, "mkdir", mkdir)
    before_fds = _open_fds()
    before = _records(library_root)
    err = fails(lib, "io_error", name="Removed Client")
    assert removed
    assert err.details["reason"] == "unstable"
    assert err.details["path"] == "Removed Client"
    assert _records(library_root) == before
    if before_fds is not None:
        assert _open_fds() == before_fds
    monkeypatch.undo()
    assert "removed-client" not in {e.id for e in lib.discover().entries}


# --- descriptor-relative errors keep their parent directory ----------------------


@native_writes
@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs a non-root POSIX user")
def test_permission_error_reports_the_full_relative_path(lib, library_root):
    client_dir = library_root / "Acme Bakery"
    for child in client_dir.iterdir():
        if child.name.casefold() == "projects":
            for path in sorted(child.rglob("*"), reverse=True):
                path.unlink() if not path.is_dir() else path.rmdir()
            child.rmdir()
    mode = stat.S_IMODE(client_dir.stat().st_mode)
    client_dir.chmod(0o555)
    try:
        err = fails(lib, "io_error", kind="project", name="Menu Refresh", client="acme-bakery")
    finally:
        client_dir.chmod(mode)
    assert err.details["errno"] == "EACCES"
    assert err.details["path"] == "Acme Bakery/Projects"


@native_writes
def test_located_errors_keep_parent_context_for_link_and_unlink(library_root):
    sub = library_root / "Sub"
    sub.mkdir()
    write(sub, "a.md", record_bytes("client", "sub-a", "Sub A"))
    with library_module._Dir.open_root(library_root) as top, top.open_dir("Sub") as held:
        with pytest.raises(FileNotFoundError) as e:
            held.unlink("missing.md")
        assert e.value.filename == str(sub / "missing.md")
        with pytest.raises(FileExistsError) as e:
            held.link("a.md", "a.md")
        assert e.value.filename == str(sub / "a.md")
        assert e.value.filename2 == str(sub / "a.md")
