"""The Windows write path reports a committed write as committed, and a failed create leaves nothing.

Every write below the record transaction goes through the backend ``engagements._winfs``
with ``_HANDLES`` set, so the same ``_WinDir`` code runs on both hosts: the native
handle backend on Windows and a protocol adapter over POSIX descriptors elsewhere.
A proxy in front of that backend fails exactly one of the attribute checks that run
after a step, which is how each defect is reached without a real Windows fault.

The record transaction above this layer must report a failure after the rename as a
``maintenance_warning`` on a successful result (spec 5.3, metadata failure policy),
never as an error: the record already changed, so an error invites a retry that
duplicates an append.
"""

import errno
import gc
import os
import stat
import warnings
from types import SimpleNamespace

import pytest
from engagements import fsutil
from engagements.errors import EngagementsError
from engagements.fsutil import rev
from engagements.library import open_library
from tools_support import call, copy_library, make_session, rev_of
from writer_support import do_update, fixed_clock, make_library, op_comm_append

pytestmark = pytest.mark.skipif(os.name != "nt" and not os.path.isdir("/proc/self/fd"),
                               reason="the protocol adapter needs /proc/self/fd")

RECORD = "Client.md"
APPENDED = "- Monthly review call"
ACME = ("Acme Bakery", "Client.md")


@pytest.fixture(autouse=True)
def collect_what_a_captured_refusal_leaves():
    """Collect the cycles a caught ``EngagementsError`` leaves behind, inside this module.

    A caught error keeps its traceback, and that traceback keeps the frames that held the
    directory and its descriptors. CPython frees such a cycle in some later collection, which
    would then close a descriptor while another test is counting them. Collecting here keeps
    this module's garbage out of the rest of the suite.
    """
    yield
    gc.collect()


def note(what):
    """Name the case in the CI log, and say which backend proved it.

    A passing test id is never printed by ``pytest -q``, while the warnings summary is, and it
    only lists tests that ran to the end. A warned test that passed is therefore the printable
    evidence that this case ran on that job, on the backend it names. Same idiom as the reparse
    attack reporter in ``test_windows_native.py``: a refusal on the runner is never silent.
    """
    backend = "native Windows backend" if os.name == "nt" else "POSIX protocol adapter"
    warnings.warn(f"{what}, on the {backend}", UserWarning, stacklevel=2)



class PosixWinfs:
    """The part of ``_winfs`` that ``_WinDir`` uses, over POSIX descriptors."""

    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000
    FILE_SHARE_READ = 1
    FILE_SHARE_WRITE = 2
    FILE_SHARE_DELETE = 4
    FILE_READ_ATTRIBUTES = 0x80
    FILE_WRITE_ATTRIBUTES = 0x100
    MUTATE_FILE_ACCESS = 0x10000 | 0x80

    @staticmethod
    def extended(path):
        return path

    @staticmethod
    def final_path(handle):
        return os.readlink(f"/proc/self/fd/{handle}")

    @staticmethod
    def parent_of(path):
        return os.path.dirname(path)

    @staticmethod
    def same_path(one, other):
        return one == other

    @staticmethod
    def identity(handle):
        info = os.fstat(handle)
        return info.st_dev, info.st_ino

    @staticmethod
    def open_held_directory(path, writable=True):
        return os.open(path, os.O_RDONLY | os.O_DIRECTORY), writable

    @staticmethod
    def duplicate(handle):
        return os.dup(handle)

    @staticmethod
    def close(handle):
        os.close(handle)

    @staticmethod
    def to_fd(handle, flags):
        return handle

    @staticmethod
    def flush(handle):
        return True

    @staticmethod
    def set_mtime(handle, seconds):
        os.utime(handle, (seconds, seconds))

    @staticmethod
    def file_info(handle):
        info = os.fstat(handle)
        return SimpleNamespace(is_reparse_point=False, is_directory=stat.S_ISDIR(info.st_mode),
                               volume=info.st_dev)

    def open_child_directory(self, parent, name, *, writable=True, create=False, open_if=False, filename=None):
        if create:
            os.mkdir(name, dir_fd=parent)
        return os.open(name, os.O_RDONLY | os.O_DIRECTORY, dir_fd=parent)

    def open_child_file(self, parent, name, access, share, *, create=False, open_if=False, filename=None):
        if access & self.GENERIC_READ and access & self.GENERIC_WRITE:
            flags = os.O_RDWR
        elif access & self.GENERIC_WRITE:
            flags = os.O_WRONLY
        else:
            flags = os.O_RDONLY
        if create:
            flags |= os.O_CREAT | os.O_EXCL
        elif open_if:
            flags |= os.O_CREAT
        return os.open(name, flags, 0o600, dir_fd=parent)

    def open_child_for_delete(self, parent, name, filename=None):
        return os.open(name, os.O_RDONLY, dir_fd=parent)

    def delete_on_close(self, handle, name):
        path = self.final_path(handle)
        if stat.S_ISDIR(os.fstat(handle).st_mode):
            os.rmdir(path)
        else:
            os.unlink(path)

    def move(self, handle, target, *, parent, replace, source_name=None, target_name=None):
        os.replace(self.final_path(handle), target, src_dir_fd=parent, dst_dir_fd=parent)

    def hard_link(self, target, handle, *, parent, target_name=None, source_name=None):
        os.link(self.final_path(handle), target, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)


class OneFault:
    """A backend proxy that fails one attribute query after a step, exactly once."""

    def __init__(self, base, *, after_move=False, after_create=False, after_link=False, before_rename=False):
        self.base = base
        self.after_move = after_move
        self.after_create = after_create
        self.after_link = after_link
        self.before_rename = before_rename
        self.renamed_record = False
        self.linked_record = False
        self.created = set()
        self.fired = False

    def __getattr__(self, name):
        return getattr(self.base, name)

    def open_child_file(self, parent, name, access, share, **kwargs):
        handle = self.base.open_child_file(parent, name, access, share, **kwargs)
        if kwargs.get("create"):
            self.created.add(handle)
        return handle

    def move(self, handle, target, *, parent, replace, source_name=None, target_name=None):
        if self.before_rename and target_name == RECORD:
            raise OSError(errno.EIO, "injected rename failure")
        self.base.move(handle, target, parent=parent, replace=replace, source_name=source_name,
                       target_name=target_name)
        if target_name == RECORD:
            self.renamed_record = True

    def hard_link(self, target, handle, **kwargs):
        result = self.base.hard_link(target, handle, **kwargs)
        if target == RECORD:
            self.linked_record = True
        return result

    def file_info(self, handle):
        if not self.fired:
            if self.after_move and self.renamed_record and self.base.file_info(handle).is_directory:
                self.fired = True
                raise OSError(errno.EIO, "injected attribute query failure after the rename")
            if self.after_link and self.linked_record and self.base.file_info(handle).is_directory:
                self.fired = True
                raise OSError(errno.EIO, "injected attribute query failure after the link")
            if self.after_create and handle in self.created:
                self.fired = True
                raise OSError(errno.EIO, "injected attribute query failure after the create")
        return self.base.file_info(handle)


def host_backend():
    """The real backend of this host: native handles on Windows, POSIX descriptors elsewhere."""
    if os.name == "nt":
        from engagements import _winfs

        return _winfs
    return PosixWinfs()


@pytest.fixture
def install(monkeypatch):
    """Put the host backend behind ``_HANDLES``, optionally with one injected failure."""

    def install_faults(**fault):
        proxy = OneFault(host_backend(), **fault)
        monkeypatch.setattr(fsutil, "_HANDLES", True)
        monkeypatch.setattr(fsutil, "_winfs", proxy)
        monkeypatch.setattr(os, "O_BINARY", getattr(os, "O_BINARY", 0), raising=False)
        return proxy

    return install_faults


def record_path(root):
    return root.joinpath(*ACME)


def temps(root):
    return sorted(path.name for path in root.rglob(".engagements-*.tmp"))


def update_through_the_tool(session, base_rev):
    return call("engagements_update", {"id": "acme-bakery", "base_rev": base_rev, "source": "test 2026-11-07",
                                       "ops": [op_comm_append(APPENDED)]}, session)


# --- a committed write is reported as committed ----------------------------------


def test_a_failed_check_after_the_rename_is_a_maintenance_warning_not_an_error(tmp_path, install):
    root = copy_library(tmp_path)
    session, _ = make_session(root)
    base_rev = call("engagements_context", {"id": "acme-bakery"}, session)["data"]["rev"]
    faults = install(after_move=True)

    result = update_through_the_tool(session, base_rev)

    assert faults.fired
    assert result["ok"] is True, result
    assert [warning["code"] for warning in result["warnings"]].count("maintenance_warning") == 1
    assert record_path(root).read_text(encoding="utf-8").count(APPENDED) == 1
    note("a failed check after the rename is a maintenance_warning, not an error")


def test_a_failed_check_after_the_rename_keeps_the_new_bytes_under_the_reported_revision(tmp_path, install):
    lib = make_library(tmp_path)
    faults = install(after_move=True)

    result = do_update(lib, rev(lib.read()), [op_comm_append(APPENDED)])

    assert faults.fired
    assert [warning["code"] for warning in result.warnings] == ["maintenance_warning"]
    assert lib.read().count(APPENDED.encode()) == 1
    assert result.data["new_rev"] == rev(lib.read())
    assert result.data["new_rev"] == rev_of(lib.path)
    assert temps(lib.root) == []
    note("a failed check after the rename keeps the new bytes under the reported revision")


def test_a_failure_before_the_rename_is_still_an_error_and_keeps_the_old_bytes(tmp_path, install):
    lib = make_library(tmp_path)
    before = lib.read()
    faults = install(before_rename=True)

    with pytest.raises(EngagementsError) as raised:
        do_update(lib, rev(before), [op_comm_append(APPENDED)])

    assert raised.value.code == "io_error"
    assert raised.value.details["path"] == "Acme Bakery/Client.md"
    assert faults.fired is False
    assert lib.read() == before
    assert temps(lib.root) == []
    note("a failure before the rename is still an error and keeps the old bytes")


# --- a failed create leaves nothing behind ---------------------------------------


def test_a_failed_check_after_the_create_leaves_no_temp_file(tmp_path, install):
    lib = make_library(tmp_path)
    before = lib.read()
    faults = install(after_create=True)

    with pytest.raises(EngagementsError) as raised:
        do_update(lib, rev(before), [op_comm_append(APPENDED)])

    assert raised.value.code == "io_error"
    assert faults.fired
    assert lib.read() == before
    assert temps(lib.root) == []
    note("a failed check after the create leaves no temp file")


def test_a_create_that_fails_on_an_existing_name_never_removes_that_file(tmp_path, install, monkeypatch):
    lib = make_library(tmp_path)
    monkeypatch.setattr(os, "urandom", lambda size: b"\x2a" * size)
    squatter = lib.path.parent / (".engagements-" + (b"\x2a" * 8).hex() + ".tmp")
    squatter.write_bytes(b"not this call's file")
    install()

    with pytest.raises(EngagementsError) as raised:
        do_update(lib, rev(lib.read()), [op_comm_append(APPENDED)])

    assert raised.value.code == "io_error"
    assert squatter.read_bytes() == b"not this call's file"
    note("a create that fails on an existing name never removes that file")


# --- a published create is reported as published ---------------------------------


def test_a_failed_check_after_the_link_still_publishes_the_new_record(library_root, install):
    faults = install(after_link=True)
    lib = open_library(library_root, create_default=False)

    result = lib.create("client", "Quarry Lane Pottery", profile="studio-dev", today=fixed_clock)

    assert faults.fired
    assert result.data()["id"] == "quarry-lane-pottery"
    assert (library_root / "Quarry Lane Pottery" / "Client.md").is_file()
    assert temps(library_root) == []
    note("a failed check after the link still publishes the new record")
