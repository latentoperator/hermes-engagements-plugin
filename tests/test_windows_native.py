"""Native Windows threat and contract tests (SPEC 5.3, TOOLS.md "Filesystem boundary").

These run only on Windows, where CI runs them on ``windows-latest``. Each proves that nothing
is written outside the library root and that the result is a controlled TOOLS.md error. Links
are created with ``_winapi.CreateJunction`` and ``os.symlink``; a runner that cannot create
symlinks skips only the symlink variants. Fictional data only.
"""

import ctypes
import errno
import os
import struct
import sys
import threading
import warnings
from ctypes import wintypes

import pytest
from conftest import clock, record_bytes, write
from engagements import fsutil
from engagements import library as library_module
from engagements.errors import EngagementsError, LockTimeout
from engagements.fsutil import FileLock, rev
from writer_support import do_update, make_library, op_status

if sys.platform == "win32":
    from engagements import _winfs
else:  # POSIX collection must not import the Win32 module at all
    _winfs = None

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="native Windows behavior")

CODES = {"io_error", "outside_root", "not_found", "excluded"}


def link_dir(link, target, kind):
    """Make ``link`` a junction or a directory symlink to ``target``."""
    if kind == "junction":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
        return
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError as exc:  # no SeCreateSymbolicLinkPrivilege on this runner
        pytest.skip(f"cannot create directory symlinks here: {exc}")


def link_file(link, target):
    try:
        os.symlink(target, link)
    except OSError as exc:
        pytest.skip(f"cannot create file symlinks here: {exc}")


def everything(path):
    return sorted(str(p.relative_to(path)) for p in path.rglob("*"))


def remove_tree(path):
    for child in sorted(path.rglob("*"), reverse=True):
        child.rmdir() if child.is_dir() else child.unlink()
    path.rmdir()


@pytest.fixture
def outside(tmp_path):
    path = tmp_path / "outside"
    path.mkdir()
    return path


@pytest.fixture
def clib(opened_lib):
    return opened_lib


def create(lib, kind="client", name="Foo Co", **kwargs):
    kwargs.setdefault("profile", "studio-dev")
    kwargs.setdefault("today", clock)
    return lib.create(kind, name, **kwargs)


def create_fails(lib, **kwargs):
    with pytest.raises(EngagementsError) as raised:
        create(lib, **kwargs)
    assert raised.value.code in CODES, (raised.value.code, raised.value.details)
    assert not os.path.isabs(str(raised.value.details.get("path", "")))
    return raised.value


# --- a link at every level of the create chain -------------------------------------------------


@pytest.mark.parametrize("kind", ["junction", "symlink"])
def test_a_link_for_the_metadata_directory_is_refused_by_create(clib, library_root, outside, kind):
    remove_tree(library_root / ".engagements")
    link_dir(library_root / ".engagements", outside, kind)
    create_fails(clib, name="Foo Co")
    assert everything(outside) == []
    assert not (library_root / "Foo Co").exists()


@pytest.mark.parametrize("kind", ["junction", "symlink"])
def test_a_link_for_the_locks_directory_is_refused_by_create(clib, library_root, outside, kind):
    locks = library_root / ".engagements" / "locks"
    if locks.exists():
        remove_tree(locks)
    link_dir(locks, outside, kind)
    create_fails(clib, name="Foo Co")
    assert everything(outside) == []
    assert not (library_root / "Foo Co").exists()


@pytest.mark.parametrize("kind", ["junction", "symlink"])
def test_a_link_for_the_projects_directory_is_refused_by_create(clib, library_root, outside, kind):
    projects = library_root / "Riverbend Dental" / "Projects"
    remove_tree(projects)
    link_dir(projects, outside, kind)
    create_fails(clib, kind="project", name="Menu Refresh", client="riverbend-dental")
    assert everything(outside) == []


@pytest.mark.parametrize("kind", ["junction", "symlink"])
def test_a_link_for_a_client_directory_is_refused_by_create(clib, library_root, outside, kind):
    client = library_root / "Riverbend Dental"
    remove_tree(client)
    link_dir(client, outside, kind)
    create_fails(clib, kind="project", name="Menu Refresh", client="riverbend-dental")
    assert everything(outside) == []


def test_a_link_for_the_lock_file_is_refused_and_creates_nothing_outside(clib, library_root, outside):
    locks = library_root / ".engagements" / "locks"
    locks.mkdir(parents=True, exist_ok=True)
    link_file(locks / "_library.lock", outside / "planted.lock")
    err = create_fails(clib, name="Foo Co")
    assert err.code == "io_error"
    assert everything(outside) == []
    assert not (library_root / "Foo Co").exists()


# --- a link at every level of the update chain -------------------------------------------------


@pytest.fixture
def wlib(tmp_path):
    return make_library(tmp_path)


def test_a_link_for_the_record_directory_cannot_redirect_an_update(wlib, outside):
    base = wlib.base()
    original = wlib.path.read_bytes()
    (outside / wlib.path.name).write_bytes(original)
    remove_tree(wlib.path.parent)
    link_dir(wlib.path.parent, outside, "junction")
    with pytest.raises(EngagementsError) as raised:
        do_update(wlib, base, [op_status()])
    assert raised.value.code in CODES
    assert everything(outside) == [wlib.path.name]
    assert (outside / wlib.path.name).read_bytes() == original


def test_a_link_for_the_record_file_is_never_followed(wlib, outside):
    base = wlib.base()
    target = outside / "target.md"
    target.write_bytes(wlib.path.read_bytes())
    wlib.path.unlink()
    link_file(wlib.path, target)
    before = target.read_bytes()
    with pytest.raises(EngagementsError) as raised:
        do_update(wlib, base, [op_status()])
    assert raised.value.code in CODES
    assert target.read_bytes() == before
    assert everything(outside) == ["target.md"]


@pytest.mark.parametrize("name", ["locks", "history", "snapshots"])
def test_a_link_inside_the_metadata_tree_cannot_redirect_an_update(wlib, outside, name):
    base = wlib.base()
    meta = wlib.root / ".engagements"
    if (meta / name).exists():
        remove_tree(meta / name)
    link_dir(meta / name, outside, "junction")
    try:
        result = do_update(wlib, base, [op_status()])
    except EngagementsError as raised:
        assert raised.code in CODES
        assert wlib.path.read_bytes().decode().count("status: paused") == 0  # refused before the replace
    else:
        # The committed-byte snapshot is post-replace maintenance (TOOLS.md failure policy): ok with a warning.
        assert name == "snapshots"
        assert [w["code"] for w in result.warnings] == ["maintenance_warning"]
    assert everything(outside) == []


# --- held directories cannot be moved or deleted while a write is in flight --------------------


def test_the_record_directory_and_root_cannot_be_renamed_during_an_update(wlib):
    base = wlib.base()
    attempts = []

    def hostile():
        for source, target in ((wlib.path.parent, wlib.root / "Moved Acme"),
                               (wlib.root, wlib.root.with_name("moved-library"))):
            try:
                os.rename(source, target)
            except OSError as exc:
                attempts.append(type(exc).__name__)
            else:  # pragma: no cover - the failure this test exists to catch
                attempts.append("renamed")
                os.rename(target, source)

    result = do_update(wlib, base, [op_status()], before_final_rehash=hostile)
    assert result.data["new_rev"]
    assert attempts == ["PermissionError", "PermissionError"], attempts
    assert wlib.path.is_file()


def test_the_client_directory_cannot_be_renamed_while_a_create_publishes(clib, library_root):
    attempts = []
    real = library_module._publish_exclusive

    def publish(directory, name, data, **kwargs):
        victim = library_root / "Foo Co"
        if not attempts and victim.is_dir():
            try:
                os.rename(victim, library_root / "Moved Foo")
            except OSError as exc:
                attempts.append(type(exc).__name__)
            else:  # pragma: no cover
                attempts.append("renamed")
        return real(directory, name, data, **kwargs)

    library_module._publish_exclusive = publish
    try:
        result = create(clib, name="Foo Co")
    finally:
        library_module._publish_exclusive = real
    assert attempts == ["PermissionError"], attempts
    assert (library_root / result.path).is_file()
    assert not (library_root / "Moved Foo").exists()


# --- the directory seam itself ------------------------------------------------------------------


def test_create_new_never_overwrites_an_existing_file(library_root):
    target = library_root / "keep.md"
    target.write_bytes(b"keep")
    with library_module._Dir.open_root(library_root) as top, pytest.raises(FileExistsError):
        top.open("keep.md", os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    assert target.read_bytes() == b"keep"


def test_create_new_refuses_a_dangling_link_and_creates_no_target(library_root, outside):
    link_file(library_root / "dangling.md", outside / "created.md")
    with library_module._Dir.open_root(library_root) as top, pytest.raises(FileExistsError):
        top.open("dangling.md", os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    assert everything(outside) == []


def test_a_link_at_the_final_component_is_not_followed_for_reading(library_root, outside):
    (outside / "secret.md").write_bytes(b"outside")
    link_file(library_root / "peek.md", outside / "secret.md")
    with library_module._Dir.open_root(library_root) as top, pytest.raises(OSError) as raised:
        top.open("peek.md", os.O_RDONLY)
    assert raised.value.errno == errno.ELOOP


@pytest.mark.parametrize("kind", ["junction", "symlink"])
def test_a_link_for_a_child_directory_is_refused_when_opened(library_root, outside, kind):
    link_dir(library_root / "Sideways", outside, kind)
    with library_module._Dir.open_root(library_root) as top, pytest.raises(fsutil._NotRealDirectory):
        top.open_dir("Sideways")


@pytest.mark.parametrize("name", ["..\\escape.md", "../escape.md", "sub\\x.md", "C:\\x.md", "a:stream", "", ".", "..",
                                  "bad*name", "line\nbreak"])
def test_a_name_that_is_not_one_plain_component_is_refused(library_root, outside, name):
    with library_module._Dir.open_root(library_root) as top, pytest.raises(OSError) as raised:
        top.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    assert raised.value.errno == errno.EINVAL
    assert everything(outside) == []
    assert not (library_root.parent / "escape.md").exists()


def test_replace_swaps_a_file_in_one_step_and_never_follows_a_link(library_root, outside):
    write(library_root, "a.md", record_bytes("client", "a-co", "A Co"))
    write(library_root, "b.md", b"new")
    (outside / "victim.md").write_bytes(b"outside")
    with library_module._Dir.open_root(library_root) as top:
        top.replace("b.md", "a.md")
    assert (library_root / "a.md").read_bytes() == b"new"
    assert not (library_root / "b.md").exists()
    link_file(library_root / "c.md", outside / "victim.md")
    write(library_root, "d.md", b"fresh")
    with library_module._Dir.open_root(library_root) as top:
        top.replace("d.md", "c.md")  # replaces the link itself, never the file it points to
    assert (outside / "victim.md").read_bytes() == b"outside"
    assert (library_root / "c.md").read_bytes() == b"fresh"
    assert not (library_root / "c.md").is_symlink()


def test_fsync_of_a_directory_is_a_documented_no_op(library_root):
    with library_module._Dir.open_root(library_root) as top:
        assert top.fsync() is None


# --- locks ---------------------------------------------------------------------------------------


def test_two_threads_exclude_each_other_and_the_loser_times_out(tmp_path):
    path = tmp_path / "x.lock"
    entered = threading.Event()
    release = threading.Event()
    errors = []

    def holder():
        try:
            with FileLock(path, timeout=5):
                entered.set()
                release.wait(10)
        except Exception as exc:  # noqa: BLE001 - reported by the assertion below
            errors.append(exc)

    thread = threading.Thread(target=holder)
    thread.start()
    assert entered.wait(10)
    with pytest.raises(LockTimeout), FileLock(path, timeout=0.3):
        pass  # pragma: no cover
    release.set()
    thread.join(10)
    assert errors == []
    with FileLock(path, timeout=1):  # free again
        pass


def test_a_timed_out_or_released_lock_leaves_no_handle_behind(tmp_path):
    path = tmp_path / "y.lock"
    with FileLock(path, timeout=1), pytest.raises(LockTimeout), FileLock(path, timeout=0.2):
        pass  # pragma: no cover
    # A leaked handle keeps the deleted file delete-pending, and Windows refuses to
    # reuse a delete-pending name. Unlink alone would not show it: these handles share
    # delete, so the delete succeeds either way.
    os.unlink(path)
    replacement = _winfs.create_file(_winfs.extended(path), _winfs.GENERIC_WRITE, 0,
                                     _winfs.CREATE_NEW, _winfs.FILE_ATTRIBUTE_NORMAL)
    _winfs.close(replacement)
    os.unlink(path)
    assert not path.exists()


def test_a_lock_at_a_link_is_refused(tmp_path, outside):
    path = tmp_path / "z.lock"
    link_file(path, outside / "planted.lock")
    with pytest.raises(EngagementsError) as raised, FileLock(path, timeout=0.2):
        pass  # pragma: no cover
    assert raised.value.code == "io_error"
    assert everything(outside) == []


def test_the_handle_backend_is_what_this_platform_reports():
    assert fsutil._HANDLES and fsutil._winfs is not None
    assert library_module._safe_writes()


# --- the created directory is held without a follow-up open -----------------------------------


def test_mkdir_returns_the_created_directory_already_held(library_root):
    """One create, no mkdir-then-open window: the returned directory is the object that was made."""
    with library_module._Dir.open_root(library_root) as top:
        child = top.mkdir("Brand New")
        try:
            assert child.relpath == "Brand New"
            fd = child.open("x.md", os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            os.close(fd)
            assert (library_root / "Brand New" / "x.md").is_file()
        finally:
            child.close()


# --- an in-place reparse mutation of a held directory cannot redirect a write -------------------
#
# The attack: the plugin holds an *empty* directory (the library root before
# `.engagements`, a fresh `locks`, client, history or `snapshots` directory) and
# another handle turns that same object into a mount point. `FSCTL_SET_REPARSE_POINT`
# needs `FILE_WRITE_DATA` or `FILE_WRITE_ATTRIBUTES`, not `DELETE` (MS-FSA
# 2.1.5.10.37), and the plugin grants write sharing because the file system opens
# the directory for write itself when a link or a rename adds a name to it, so
# the attack is expected to succeed on an empty directory. The confinement comes
# from the operations being object-relative: the kernel refuses to resolve a
# relative name through the reparse-pointed directory and nothing lands outside
# (see `test_a_mounted_held_directory_cannot_resolve_a_relative_child`). Each
# test below pauses the operation at the `fsutil._before_child_mutation` seam,
# applies a real mount-point buffer, resumes, and asserts that nothing appeared
# outside. The report says what happened to the reparse attempt, so a runner that
# refuses it is visible rather than silent.

FSCTL_SET_REPARSE_POINT = 0x000900A4
FSCTL_DELETE_REPARSE_POINT = 0x000900AC
IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003
_SHARE_ALL = 0x1 | 0x2 | 0x4  # FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE

_control = ctypes.WinDLL("kernel32", use_last_error=True) if sys.platform == "win32" else None
if _control is not None:
    _control.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                         ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                         ctypes.c_void_p]
    _control.DeviceIoControl.restype = wintypes.BOOL


def _absolute(path):
    text = os.path.abspath(str(path))
    return text if text.startswith("\\\\?\\") else "\\\\?\\" + text


def mount_point_buffer(target):
    """A ``REPARSE_DATA_BUFFER`` that makes a directory a mount point for ``target``.

    Both names are stored NUL-terminated and ``ReparseDataLength`` covers those
    terminators, which is what ``DeviceIoControl`` validates (the same layout
    CPython's ``_winapi.CreateJunction`` builds).
    """
    substitute = ("\\??\\" + _absolute(target)[4:]).encode("utf-16-le")
    printed = _absolute(target)[4:].encode("utf-16-le")
    data = (struct.pack("<HHHH", 0, len(substitute), len(substitute) + 2, len(printed))
            + substitute + b"\x00\x00" + printed + b"\x00\x00")
    return struct.pack("<IHH", IO_REPARSE_TAG_MOUNT_POINT, len(data), 0) + data


def apply_mount_point(handle, target):
    """Set a real mount-point reparse point through ``handle``; raises when the kernel refuses it."""
    data = mount_point_buffer(target)
    returned = wintypes.DWORD()
    if not _control.DeviceIoControl(handle, FSCTL_SET_REPARSE_POINT, data, len(data),
                                    None, 0, ctypes.byref(returned), None):
        raise ctypes.WinError(ctypes.get_last_error())


def remove_mount_point(handle):
    data = struct.pack("<IHH", IO_REPARSE_TAG_MOUNT_POINT, 0, 0)
    returned = wintypes.DWORD()
    _control.DeviceIoControl(handle, FSCTL_DELETE_REPARSE_POINT, data, len(data),
                             None, 0, ctypes.byref(returned), None)


def attack_held_directory(directory, outside):
    """Try what another process would: open the held directory for write and make it a mount point."""
    report = {"directory": directory.relpath or ".", "set": False, "outcome": ""}
    try:
        handle = _winfs.create_file(_winfs.extended(directory.path),
                                    _winfs.GENERIC_READ | _winfs.GENERIC_WRITE,
                                    _winfs.FILE_SHARE_READ | _winfs.FILE_SHARE_WRITE | _winfs.FILE_SHARE_DELETE,
                                    _winfs.OPEN_EXISTING,
                                    _winfs.FILE_FLAG_BACKUP_SEMANTICS | _winfs.FILE_FLAG_OPEN_REPARSE_POINT)
    except OSError as exc:
        report["outcome"] = f"refused at open ({type(exc).__name__}, winerror={getattr(exc, 'winerror', None)})"
        return report
    try:
        apply_mount_point(handle, outside)
        report["set"] = True
        # Confirm from the object itself that it now carries the reparse tag.
        report["tagged"] = bool(_winfs.file_info(handle).is_reparse_point)
        report["outcome"] = f"mount point set (reparse attribute present: {report['tagged']})"
    except OSError as exc:
        report["outcome"] = f"mount point refused ({type(exc).__name__}, winerror={getattr(exc, 'winerror', None)})"
    finally:
        _winfs.close(handle)
    return report


@pytest.fixture
def in_place_attack(monkeypatch):
    """Plan one attack on a held directory; it fires once, at the mutation seam."""
    plans = []
    reports = []

    def plan(relpath, matches, outside):
        plans.append({"relpath": relpath, "matches": matches, "outside": outside, "done": False})

    def hook(directory, name):
        for item in plans:
            if item["done"] or (directory.relpath or "") != item["relpath"] or not item["matches"](name):
                continue
            item["done"] = True
            reports.append(attack_held_directory(directory, item["outside"]))
            break

    monkeypatch.setattr(fsutil, "_before_child_mutation", hook)
    return plan, reports


def record_attack(reports):
    """Surface what the reparse attempt did, so a refusal on the runner is never silent."""
    assert reports, "the operation never reached the held-directory mutation seam"
    for report in reports:
        warnings.warn(f"in-place reparse attack on {report['directory']}: {report['outcome']}",
                      UserWarning, stacklevel=2)
        # An attack that never set its mount point proves nothing; fail rather than pass with a warning.
        assert report["set"] and report.get("tagged"), report


def test_a_reparse_point_on_the_held_root_cannot_redirect_first_use(tmp_path, outside, in_place_attack):
    """The empty root is held, turned into a mount point, and then `.engagements` is created."""
    plan, reports = in_place_attack
    root = tmp_path / "empty-library"
    root.mkdir()
    plan("", lambda name: name == ".engagements", outside)
    try:
        library_module.open_library(root, create_default=True)
    except EngagementsError as exc:
        assert exc.code in CODES, (exc.code, exc.details)
    assert everything(outside) == []
    record_attack(reports)


def test_a_reparse_point_on_the_held_locks_directory_cannot_redirect_the_lock_file(tmp_path, outside,
                                                                                 in_place_attack):
    """A fresh `locks` directory is held, turned into a mount point, then the lock file is opened."""
    plan, reports = in_place_attack
    root = tmp_path / "empty-library"
    root.mkdir()
    plan(".engagements/locks", lambda name: name == "_library.lock", outside)
    try:
        library_module.open_library(root, create_default=True)
    except EngagementsError as exc:
        assert exc.code in CODES, (exc.code, exc.details)
    assert everything(outside) == []
    record_attack(reports)


def test_a_reparse_point_on_a_held_client_directory_cannot_redirect_the_record(clib, library_root, outside,
                                                                              in_place_attack):
    """A freshly created client directory is held when the first record write begins."""
    plan, reports = in_place_attack
    plan("Foo Co", lambda name: name.startswith(".engagements-new-"), outside)
    try:
        result = create(clib, name="Foo Co")
    except EngagementsError as exc:
        assert exc.code in CODES, (exc.code, exc.details)
    else:
        assert (library_root / result.path).is_file()
    assert everything(outside) == []
    record_attack(reports)


def test_a_reparse_point_on_a_held_history_directory_cannot_redirect_the_history_copy(wlib, outside,
                                                                                    in_place_attack):
    """A fresh `history` directory is held while the pre-update copy is written into it."""
    plan, reports = in_place_attack
    base = wlib.base()
    plan(".engagements/history", lambda name: not name.startswith("."), outside)
    with pytest.raises(EngagementsError) as raised:
        do_update(wlib, base, [op_status()])
    assert raised.value.code in CODES, (raised.value.code, raised.value.details)
    assert everything(outside) == []
    record_attack(reports)


def test_a_reparse_point_on_a_held_snapshot_directory_cannot_redirect_the_snapshot(wlib, outside,
                                                                                 in_place_attack):
    """A fresh `snapshots` directory is held while the post-replace snapshot is written."""
    plan, reports = in_place_attack
    # Take the base revision straight from the bytes: a plugin read would write a
    # read-time snapshot first, and the attack needs the directory to still be empty.
    base = rev(wlib.read())
    plan(".engagements/snapshots", lambda name: name.startswith(".engagements-"), outside)
    try:
        do_update(wlib, base, [op_status()])
    except EngagementsError as exc:
        assert exc.code in CODES, (exc.code, exc.details)
    assert everything(outside) == []
    record_attack(reports)


def test_a_mounted_held_directory_cannot_resolve_a_relative_child(tmp_path, outside):
    """The kernel, not the plugin's own check, is what confines the write.

    This is the property the no-outside-write contract rests on, so it is shown
    directly: once a held empty directory has been turned into a mount point, a
    raw object-relative create, open, link and rename through that held handle
    all fail inside the kernel and nothing appears in the mounted target. The
    plugin's attribute re-check normally reports the same condition first, as
    ``reparse_point``.

    The mount point goes on an empty child directory because Windows refuses a
    name-surrogate tag on a non-empty one (WinError 145); the source of the link
    and rename lives in the parent, which is held for the whole test.
    """
    base = tmp_path / "held"
    base.mkdir()
    (base / "Source.tmp").write_bytes(b"renamed bytes")
    (outside / "Existing.md").write_bytes(b"outside bytes")
    with library_module._Dir.open_root(base) as top:
        sub = top.mkdir("Sub")
        try:
            source = _winfs.open_child_file(top.handle, "Source.tmp", _winfs.DELETE, _SHARE_ALL)
            try:
                apply_mount_point(sub.handle, outside)
                attempts = {}

                def attempt(label, fn):
                    try:
                        handle = fn()
                    except OSError as exc:
                        attempts[label] = getattr(exc, "winerror", None) or exc.errno
                    else:
                        _winfs.close(handle)
                        attempts[label] = "succeeded"

                try:
                    attempt("create", lambda: _winfs.open_child_file(
                        sub.handle, "Raw.md", _winfs.GENERIC_READ | _winfs.GENERIC_WRITE, _SHARE_ALL,
                        create=True))
                    attempt("open", lambda: _winfs.open_child_file(
                        sub.handle, "Existing.md", _winfs.GENERIC_READ, _SHARE_ALL))
                    attempt("link", lambda: _winfs.hard_link(
                        "Linked.md", source, parent=sub.handle, target_name="Linked.md"))
                    attempt("rename", lambda: _winfs.move(
                        source, "Renamed.md", parent=sub.handle, replace=True))
                finally:
                    remove_mount_point(sub.handle)
            finally:
                _winfs.close(source)
        finally:
            sub.close()
    warnings.warn(f"relative attempts through a mounted held directory: {attempts}", UserWarning, stacklevel=1)
    assert all(outcome != "succeeded" for outcome in attempts.values()), attempts
    assert everything(outside) == ["Existing.md"]


def test_a_held_directory_that_became_a_reparse_point_fails_the_next_mutation_closed(tmp_path, outside):
    """Defense in depth: the attribute re-check reports the change as `reparse_point`."""
    root = tmp_path / "plain"
    root.mkdir()
    with library_module._Dir.open_root(root) as top:
        apply_mount_point(top.handle, outside)  # possible here only because our own handle may write
        try:
            with pytest.raises((fsutil._Refusal, OSError)) as raised:
                top.mkdir("Planted")
            assert "reparse" in str(raised.value).lower() or getattr(raised.value, "reason", "") == "reparse_point"
        finally:
            remove_mount_point(top.handle)
    assert everything(outside) == []
    assert not (root / "Planted").exists()


def _short_name(path):
    """The 8.3 alias of ``path``'s last component, or None when the volume does not generate them."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetShortPathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    kernel.GetShortPathNameW.restype = wintypes.DWORD
    buffer = ctypes.create_unicode_buffer(1024)
    if not kernel.GetShortPathNameW(str(path), buffer, len(buffer)):
        return None
    short = os.path.basename(buffer.value)
    return short if short.casefold() != os.path.basename(str(path)).casefold() else None


def test_a_short_name_alias_resolves_only_to_the_entry_in_the_held_directory(tmp_path):
    """An 8.3 alias is a second name for an entry of the same directory: it can neither leave
    the held directory nor create a second file over an existing one."""
    root = tmp_path / "lib"
    root.mkdir()
    long_file = root / "a long record name.md"
    long_file.write_bytes(b"original")
    short = _short_name(long_file)
    if short is None:
        pytest.skip("8.3 short names are not generated on this volume")
    with fsutil._WinDir.open_root(root) as held:
        fd = held.open(short, os.O_RDONLY)
        try:
            assert os.fstat(fd).st_ino == os.stat(long_file).st_ino
        finally:
            os.close(fd)
        with pytest.raises(FileExistsError):
            held.open(short, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    assert sorted(os.listdir(root)) == ["a long record name.md"]
    assert long_file.read_bytes() == b"original"


@pytest.mark.parametrize("name", ["CONIN$", "CONOUT$", "conin$.md"])
def test_console_device_names_never_reach_the_kernel(tmp_path, name):
    root = tmp_path / "lib"
    root.mkdir()
    with fsutil._WinDir.open_root(root) as held, pytest.raises(OSError) as raised:
        held.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    assert raised.value.errno == errno.EINVAL
    assert os.listdir(root) == []
