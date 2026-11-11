"""Descriptor acquisition inside the writer's error mapping, private history and snapshot copies,
the required history directory sync, and a relative root with a deleted current directory.
Fictional data only.

``_open_record_dir`` and ``_meta_dir`` used to duplicate the root descriptor before
their error mapping, so an ``os.dup`` failure (EMFILE) escaped as a raw
``OSError``: from a read instead of ``snapshot_failed``, and from an update
whose record had already been committed instead of a ``maintenance_warning``.

Temporary files were created 0666, so history and snapshot copies of a
private (0600) record became group/other readable under the umask. They are
created 0600 again; only the record's own temp gets the record's mode.

The required pre-update history copy synced its directory through a helper
that swallowed every ``OSError``, so a real EIO no longer aborted the update
before the replace.

``open_library`` resolved a relative root outside its OSError
mapping, so a deleted current directory leaked ``FileNotFoundError``.

Faults are injected at the real system call (``os.dup``, ``os.fsync``,
``os.fchmod``) while a named writer stage is on the stack; every other
filesystem call is real.
"""

import errno
import os
import stat

import pytest
from engagements import fsutil, writer
from engagements import library as library_module
from engagements.errors import EngagementsError
from engagements.library import open_library
from writer_support import do_update, fixed_clock, make_library, op_status

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX descriptors and modes")


def fail_dup_during(monkeypatch, stage, when=None, err=errno.EMFILE):
    """Make ``os.dup`` raise ``err`` only while ``writer.<stage>`` runs (and ``when(args)`` holds)."""
    real_stage, real_dup = getattr(writer, stage), os.dup
    active, fired = [], []

    def staged(*args, **kwargs):
        armed = when is None or when(args)
        active.append(armed)
        try:
            return real_stage(*args, **kwargs)
        finally:
            active.pop()

    def dup(fd):
        if active and active[-1]:
            fired.append(fd)
            raise OSError(err, os.strerror(err))
        return real_dup(fd)

    monkeypatch.setattr(writer, stage, staged)
    monkeypatch.setattr(os, "dup", dup)
    return fired


def fd_count():
    return len(os.listdir("/proc/self/fd"))


needs_proc = pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="needs /proc/self/fd")


# ---- descriptor acquisition is inside the error mapping and the stage policy ----------


def test_read_snapshot_dup_failure_returns_the_read_with_snapshot_failed(tmp_path, monkeypatch):
    lib = make_library(tmp_path)
    fired = fail_dup_during(monkeypatch, "_write_snapshot")
    warnings = []
    data, revision = writer.read_record(lib.root, lib.path, warnings=warnings)
    assert fired
    assert data == lib.read() and revision == fsutil.rev(data)
    assert [w["code"] for w in warnings] == ["snapshot_failed"]


def test_post_commit_snapshot_dup_failure_is_a_committed_success_with_a_warning(tmp_path, monkeypatch):
    lib = make_library(tmp_path)
    base = lib.base()
    original = lib.read()
    fired = fail_dup_during(monkeypatch, "_write_snapshot")
    result = do_update(lib, base, [op_status()])
    assert fired
    assert lib.read() != original and b"status: paused" in lib.read()  # the record was committed
    assert result.data["new_rev"] == fsutil.rev(lib.read())
    assert [w["code"] for w in result.warnings] == ["maintenance_warning"]
    assert not (lib.meta / "snapshots" / f"{result.data['new_rev']}.md").exists()


@pytest.mark.parametrize("call", ["read", "update"])
def test_record_directory_dup_failure_is_io_error(tmp_path, monkeypatch, call):
    lib = make_library(tmp_path)
    base = lib.base()
    original = lib.read()
    fired = fail_dup_during(monkeypatch, "_open_record_dir")
    with pytest.raises(EngagementsError) as exc:
        if call == "read":
            writer.read_record(lib.root, lib.path)
        else:
            do_update(lib, base, [op_status()])
    assert fired
    assert exc.value.code == "io_error"
    assert not os.path.isabs(exc.value.details["path"])
    assert lib.read() == original


@pytest.mark.parametrize("parts", [("locks",), ("history", "acme-bakery")], ids=["locks", "history"])
def test_pre_commit_metadata_dup_failure_is_io_error_and_leaves_the_record(tmp_path, monkeypatch, parts):
    lib = make_library(tmp_path)
    base = lib.base()
    original = lib.read()
    fired = fail_dup_during(monkeypatch, "_meta_dir", when=lambda args: tuple(args[1:]) == parts)
    with pytest.raises(EngagementsError) as exc:
        do_update(lib, base, [op_status()])
    assert fired
    assert exc.value.code == "io_error"
    assert lib.read() == original and lib.history() == []


@needs_proc
def test_dup_failures_leak_no_descriptors(tmp_path, monkeypatch):
    lib = make_library(tmp_path)
    base = lib.base()
    start = fd_count()
    with monkeypatch.context() as patch:
        fail_dup_during(patch, "_open_record_dir")
        with pytest.raises(EngagementsError):
            do_update(lib, base, [op_status()])
    with monkeypatch.context() as patch:
        fail_dup_during(patch, "_write_snapshot")
        do_update(lib, base, [op_status()])
    assert fd_count() == start


def test_directory_close_failure_after_commit_does_not_fail_the_update(tmp_path, monkeypatch):
    """A directory descriptor's close carries no data; it must not turn a committed update into an error."""
    lib = make_library(tmp_path)
    base = lib.base()
    real_close, real_update_held = os.close, writer._update_held
    armed, failed = [], []

    def close(fd):
        directory = armed and stat.S_ISDIR(os.fstat(fd).st_mode)
        real_close(fd)  # POSIX releases the descriptor even when close reports an error
        if directory:
            failed.append(fd)
            raise OSError(errno.EIO, os.strerror(errno.EIO))

    def update_held(*args, **kwargs):
        result = real_update_held(*args, **kwargs)
        armed.append(True)  # every directory close from here on is after the commit
        return result

    monkeypatch.setattr(os, "close", close)
    monkeypatch.setattr(writer, "_update_held", update_held)
    result = do_update(lib, base, [op_status()])
    monkeypatch.undo()
    assert failed
    assert b"status: paused" in lib.read()
    assert result.data["new_rev"] == fsutil.rev(lib.read())


@needs_proc
def test_root_identity_failure_closes_the_root_descriptor(tmp_path, monkeypatch):
    lib = make_library(tmp_path)
    opened = open_library(lib.root, create_default=False)
    start = fd_count()
    real_fstat = os.fstat

    def fstat(fd):
        if stat.S_ISDIR(real_fstat(fd).st_mode):
            raise OSError(errno.EIO, os.strerror(errno.EIO))
        return real_fstat(fd)

    monkeypatch.setattr(fsutil.os, "fstat", fstat)
    with pytest.raises(EngagementsError) as exc:
        writer.read_record(opened, lib.path)
    monkeypatch.setattr(fsutil.os, "fstat", real_fstat)
    assert exc.value.code == "io_error"
    assert fd_count() == start


# ---- metadata copies are private -----------------------------------------------


@pytest.fixture
def umask_022():
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)


def private(path):
    return stat.S_IMODE(os.stat(path).st_mode) & 0o077 == 0


def test_history_and_snapshots_of_a_private_record_stay_private(tmp_path, umask_022):
    lib = make_library(tmp_path)
    os.chmod(lib.path, 0o600)
    base = lib.base()
    read_snapshot = lib.meta / "snapshots" / f"{base}.md"
    assert read_snapshot.read_bytes() == lib.read()
    assert private(read_snapshot)
    result = do_update(lib, base, [op_status()])
    assert stat.S_IMODE(os.stat(lib.path).st_mode) == 0o600
    [history] = lib.history()
    assert private(history)
    committed = lib.meta / "snapshots" / f"{result.data['new_rev']}.md"
    assert committed.read_bytes() == lib.read()
    assert private(committed)


def test_a_shared_record_keeps_its_own_mode(tmp_path, umask_022):
    lib = make_library(tmp_path)
    os.chmod(lib.path, 0o644)
    do_update(lib, lib.base(), [op_status()])
    assert stat.S_IMODE(os.stat(lib.path).st_mode) == 0o644


def test_failed_mode_preservation_never_widens_a_private_record(tmp_path, monkeypatch, umask_022):
    lib = make_library(tmp_path)
    os.chmod(lib.path, 0o600)
    base = lib.base()

    def fchmod(fd, mode):
        raise OSError(errno.EPERM, os.strerror(errno.EPERM))

    monkeypatch.setattr(fsutil.os, "fchmod", fchmod)
    do_update(lib, base, [op_status()])
    assert b"status: paused" in lib.read()
    assert private(lib.path)


# ---- the required history copy's directory sync is checked ----------------------


def fail_history_dir_fsync(monkeypatch, err):
    real_fsync = os.fsync
    fired = []

    def fsync(fd):
        target = os.readlink(f"/proc/self/fd/{fd}")
        if os.path.isdir(target) and target.endswith(os.path.join("history", "acme-bakery")):
            fired.append(target)
            raise OSError(err, os.strerror(err))
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    return fired


@needs_proc
def test_history_directory_eio_aborts_before_the_replace(tmp_path, monkeypatch):
    lib = make_library(tmp_path)
    base = lib.base()
    original = lib.read()
    fired = fail_history_dir_fsync(monkeypatch, errno.EIO)
    with pytest.raises(EngagementsError) as exc:
        do_update(lib, base, [op_status()])
    assert fired
    assert exc.value.code == "io_error"
    assert not os.path.isabs(exc.value.details["path"])
    assert lib.read() == original


@needs_proc
@pytest.mark.parametrize("err", [errno.EINVAL, errno.ENOTSUP], ids=["EINVAL", "ENOTSUP"])
def test_unsupported_history_directory_sync_still_updates(tmp_path, monkeypatch, err):
    lib = make_library(tmp_path)
    base = lib.base()
    fired = fail_history_dir_fsync(monkeypatch, err)
    result = do_update(lib, base, [op_status()])
    assert fired
    assert b"status: paused" in lib.read()
    assert result.warnings == []
    assert len(lib.history()) == 1


# ---- a relative root with a deleted current directory --------------------------


@pytest.mark.parametrize("create_default", [True, False])
def test_relative_root_with_a_deleted_cwd_is_io_error(tmp_path, create_default):
    gone = tmp_path / "gone"
    gone.mkdir()
    saved = os.open(".", os.O_RDONLY)
    try:
        os.chdir(gone)
        os.rmdir(gone)
        with pytest.raises(EngagementsError) as exc:
            open_library("relative-library", create_default=create_default)
    finally:
        os.fchdir(saved)
        os.close(saved)
    assert exc.value.code == "io_error"
    assert exc.value.details["path"] == "relative-library"
    assert not os.path.isabs(exc.value.details["path"])
    assert not (tmp_path / "relative-library").exists()


# ---- a parent close failure cannot strand the returned handle ------


@needs_proc
def test_create_survives_a_parent_directory_close_failure_without_leaking(tmp_path, monkeypatch):
    root = tmp_path / "library"
    root.mkdir()
    lib = open_library(root, create_default=True)
    start = fd_count()
    real_close, real_meta_dir = os.close, library_module.Library._meta_dir
    armed = []

    def close(fd):
        failing = armed and stat.S_ISDIR(os.fstat(fd).st_mode)
        real_close(fd)
        if failing:
            armed.clear()
            raise OSError(errno.EIO, os.strerror(errno.EIO))

    def meta_dir(self, name):
        armed.append(True)  # the first directory closed inside is a parent of the returned handle
        try:
            return real_meta_dir(self, name)
        finally:
            armed.clear()

    monkeypatch.setattr(fsutil.os, "close", close)
    monkeypatch.setattr(library_module.Library, "_meta_dir", meta_dir)
    result = lib.create("client", "Riverbend Dental", profile="studio-dev", today=fixed_clock)
    monkeypatch.undo()
    assert (root / result.path).is_file()
    assert fd_count() == start
