"""Writer lock, snapshot and record-path safety.

1. A symlink at the record lock path never creates or touches its target.
2. A FIFO snapshot or record path never hangs a read (bounded subprocesses).
3. A filesystem failure on the final re-hash is ``io_error``, not a conflict.
4. A proposed client that disappears while waiting for the lock is ``not_found``
   before the revision check.
5. A dot-prefixed record file name is a record; only dot-directories are excluded.
"""
import errno
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from engagements import writer
from engagements.errors import EngagementsError
from engagements.fsutil import FileLock, rev
from engagements.records import parse
from writer_support import (
    ACME,
    WEBSITE,
    do_update,
    fixed_clock,
    make_library,
    op_comm_append,
    op_status,
    tree_snapshot,
)

ROOT = Path(__file__).resolve().parent.parent
POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="symlinks, FIFOs and permissions are POSIX-specific")

# A child process runs each potentially blocking call, so a regression fails in
# bounded time instead of hanging the suite.
CHILD = """
import json, sys
sys.path.insert(0, sys.argv[1])
sys.path.insert(0, sys.argv[1] + '/tests')
from pathlib import Path
from engagements import writer
from engagements.errors import EngagementsError
from writer_support import fixed_clock, client_exists

action, root, path, base = sys.argv[2], Path(sys.argv[3]), Path(sys.argv[4]), sys.argv[5]
try:
    if action == 'read':
        warnings = []
        data, revision = writer.read_record(root, path, warnings=warnings)
        print(json.dumps({'ok': True, 'rev': revision, 'warnings': [w['code'] for w in warnings]}))
    elif action == 'load':
        print(json.dumps({'ok': True, 'snapshot': writer.load_snapshot(root, base) is not None}))
    else:
        result = writer.update(root, path, base, 'child', [{'op': 'set_property', 'key': 'status', 'value': 'paused'}],
                               kind='client', profile='p', clock=fixed_clock, client_exists=client_exists)
        print(json.dumps({'ok': True, 'rebased': result.data['rebased']}))
except EngagementsError as exc:
    print(json.dumps({'ok': False, 'code': exc.code, 'details': exc.details}))
"""


def bounded(action, lib, path=None, base="-"):
    completed = subprocess.run(
        [sys.executable, "-c", CHILD, str(ROOT), action, str(lib.root), str(path or lib.path), base],
        capture_output=True, text=True, timeout=15, check=False)
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


def lock_path(lib):
    return lib.meta / "locks" / "acme-bakery.lock"


# ---- 1. lock-file symlinks ------------------------------------------------------


@POSIX_ONLY
def test_dangling_lock_symlink_is_io_error_and_creates_nothing_outside(lib, tmp_path):
    outside = tmp_path / "outside-target.lock"
    lock_path(lib).parent.mkdir(parents=True)
    lock_path(lib).symlink_to(outside)
    base = lib.base()
    before = tree_snapshot(lib.root)
    with pytest.raises(EngagementsError) as exc:
        do_update(lib, base, [op_status()])
    assert exc.value.code == "io_error"
    assert exc.value.details["path"] == ".engagements/locks/acme-bakery.lock"  # relative, never absolute
    assert not outside.exists() and not os.path.lexists(outside)
    assert lock_path(lib).is_symlink()  # left as found
    assert tree_snapshot(lib.root) == before and lib.history() == []


@POSIX_ONLY
def test_existing_outside_lock_symlink_is_io_error_and_target_is_untouched(lib, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside bytes")
    stamp = outside.stat()
    lock_path(lib).parent.mkdir(parents=True)
    lock_path(lib).symlink_to(outside)
    original = lib.read()
    with pytest.raises(EngagementsError) as exc:
        do_update(lib, lib.base(), [op_status()])
    assert exc.value.code == "io_error"
    assert outside.read_bytes() == b"outside bytes"
    assert (outside.stat().st_mtime_ns, outside.stat().st_size) == (stamp.st_mtime_ns, stamp.st_size)
    assert lib.read() == original


@POSIX_ONLY
def test_file_lock_refuses_symlinks_and_special_files_directly(tmp_path):
    outside = tmp_path / "never-created"
    link = tmp_path / "dangling.lock"
    link.symlink_to(outside)
    with pytest.raises(EngagementsError) as exc, FileLock(link, timeout=0.1):
        pass
    assert exc.value.code == "io_error" and not os.path.lexists(outside)
    fifo = tmp_path / "fifo.lock"
    os.mkfifo(fifo)
    with pytest.raises(EngagementsError) as exc, FileLock(fifo, timeout=0.1):
        pass
    assert exc.value.code == "io_error"


def test_a_regular_lock_file_still_takes_a_fresh_flock_per_acquisition(lib):
    do_update(lib, lib.base(), [op_status("paused")])
    assert lock_path(lib).is_file() and not lock_path(lib).is_symlink()
    do_update(lib, lib.base(), [op_status("former")])  # a second acquisition of the same file
    with FileLock(lock_path(lib), timeout=0.1) as held:
        with pytest.raises(EngagementsError) as exc, FileLock(lock_path(lib), timeout=0.1):
            pass
        assert exc.value.code == "lock_timeout"
        assert held._file is not None
    assert parse(lib.read()).properties["status"] == "former"


# ---- 2. FIFOs never hang a read; a swapped target is still a change -------------------------------------------------


@POSIX_ONLY
def test_fifo_snapshot_does_not_hang_a_read_and_is_repaired(lib):
    revision = rev(ACME.encode())
    snapshots = lib.meta / "snapshots"
    snapshots.mkdir()
    os.mkfifo(snapshots / f"{revision}.md")
    outcome = bounded("read", lib)
    assert outcome == {"ok": True, "rev": revision, "warnings": []}
    assert (snapshots / f"{revision}.md").is_file()  # replaced by a valid snapshot
    assert writer.load_snapshot(lib.root, revision) == ACME.encode()


@POSIX_ONLY
def test_fifo_snapshot_loads_as_absent_without_hanging(lib):
    revision = "0123456789abcdef"
    snapshots = lib.meta / "snapshots"
    snapshots.mkdir()
    os.mkfifo(snapshots / f"{revision}.md")
    assert bounded("load", lib, base=revision) == {"ok": True, "snapshot": False}


@POSIX_ONLY
def test_stale_update_against_a_fifo_base_is_base_unavailable_without_hanging(lib):
    stale = lib.base()
    lib.edit("status: active", "status: paused")
    snapshot = lib.meta / "snapshots" / f"{stale}.md"
    snapshot.unlink()
    os.mkfifo(snapshot)
    outcome = bounded("update", lib, base=stale)
    assert outcome["ok"] is False and outcome["code"] == "conflict"
    assert outcome["details"]["reason"] == "base_unavailable"


@POSIX_ONLY
def test_fifo_at_a_record_path_is_excluded_without_hanging(lib):
    fifo = lib.path.with_name("Pipe.md")
    os.mkfifo(fifo)
    outcome = bounded("read", lib, path=fifo)
    assert outcome["ok"] is False
    assert (outcome["code"], outcome["details"]["reason"]) == ("not_found", "excluded")


@POSIX_ONLY
def test_symlink_swapped_in_before_the_final_rehash_is_changed_during_write(lib, tmp_path):
    elsewhere = tmp_path / "elsewhere.md"
    elsewhere.write_bytes(ACME.encode())

    def swap():
        lib.path.unlink()
        lib.path.symlink_to(elsewhere)

    with pytest.raises(EngagementsError) as exc:
        do_update(lib, lib.base(), [op_status()], before_final_rehash=swap)
    assert exc.value.code == "conflict" and exc.value.details["reason"] == "changed_during_write"
    assert exc.value.details["current_rev"] is None
    assert lib.leftovers() == []


# ---- 3. final re-hash I/O failures are io_error ---------------------------------


@pytest.mark.parametrize("code", [errno.EIO, errno.EACCES])
def test_injected_final_rehash_failure_is_io_error(lib, monkeypatch, code):
    original = lib.read()
    state = {"armed": False, "hits": 0}
    real_open = os.open
    target = os.path.realpath(lib.path)
    target_dir = os.path.dirname(target)

    def failing_open(path, flags, *args, **kwargs):
        # The writer opens the record relative to its held directory.
        opened = os.fspath(path) if kwargs.get("dir_fd") is None else os.path.join(target_dir, os.fspath(path))
        if state["armed"] and opened == target:
            state["hits"] += 1
            raise OSError(code, os.strerror(code))
        return real_open(path, flags, *args, **kwargs)

    base = lib.base()
    if sys.platform == "win32":
        from engagements import _winfs

        real_open_child = _winfs.open_child_file
        leaf = os.path.basename(target)

        def failing_open_child(parent, name, access, share, **kwargs):
            # The writer opens the record object-relative to its
            # held directory: there is no pathname to intercept, so the injection
            # sits on the NT child open the Windows backend actually makes.
            if state["armed"] and name == leaf:
                state["hits"] += 1
                raise OSError(code, os.strerror(code), name)
            return real_open_child(parent, name, access, share, **kwargs)

        monkeypatch.setattr(_winfs, "open_child_file", failing_open_child)
    else:
        monkeypatch.setattr(writer.os, "open", failing_open)
    with pytest.raises(EngagementsError) as exc:
        do_update(lib, base, [op_status()], before_final_rehash=lambda: state.update(armed=True))
    monkeypatch.undo()
    assert state["hits"] == 1  # only the final re-hash open failed
    assert exc.value.code == "io_error"
    assert exc.value.details == {"path": "Acme Bakery/Client.md"}
    assert lib.read() == original and lib.leftovers() == []


@POSIX_ONLY
@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores file permissions")
def test_real_permission_loss_before_the_final_rehash_is_io_error(lib):
    original = lib.read()
    try:
        with pytest.raises(EngagementsError) as exc:
            do_update(lib, lib.base(), [op_status()], before_final_rehash=lambda: os.chmod(lib.path, 0))
    finally:
        os.chmod(lib.path, 0o644)
    assert exc.value.code == "io_error"
    assert lib.read() == original and lib.leftovers() == []


def test_deleted_target_before_the_final_rehash_is_still_changed_during_write(lib):
    with pytest.raises(EngagementsError) as exc:
        do_update(lib, lib.base(), [op_status()], before_final_rehash=lambda: lib.path.unlink())
    assert (exc.value.code, exc.value.details["reason"]) == ("conflict", "changed_during_write")


# ---- 4. proposed client re-validated under the lock -----------------------------


def project_update(tmp_path, monkeypatch, *, riverbend_survives):
    lib = make_library(tmp_path, WEBSITE, name="Website 2026.md", folder="Projects")
    writer.read_record(lib.root, lib.path)
    known = {"acme-bakery", "riverbend-dental"}

    class Hooked(FileLock):
        def __enter__(self):
            result = super().__enter__()
            if not riverbend_survives:
                known.discard("riverbend-dental")  # the proposed client disappears while we wait
            return result

    monkeypatch.setattr(writer, "FileLock", Hooked)
    unavailable = "0123456789abcdef"  # a stale base with no snapshot
    original = lib.read()
    with pytest.raises(EngagementsError) as exc:
        writer.update(lib.root, lib.path, unavailable, "s",
                      [{"op": "set_property", "key": "client", "value": "riverbend-dental"}],
                      kind="project", profile="p", clock=fixed_clock, client_exists=lambda value: value in known)
    assert lib.read() == original
    return exc.value


def test_proposed_client_disappearing_under_the_lock_is_not_found_before_the_revision_check(tmp_path, monkeypatch):
    error = project_update(tmp_path, monkeypatch, riverbend_survives=False)
    assert (error.code, error.details) == ("not_found", {"reason": "no_match"})


def test_surviving_proposed_client_reaches_the_revision_check(tmp_path, monkeypatch):
    error = project_update(tmp_path, monkeypatch, riverbend_survives=True)
    assert (error.code, error.details["reason"]) == ("conflict", "base_unavailable")


# ---- 5. dot-prefixed file names are records -------------------------------------


def test_dot_prefixed_record_file_reads_and_updates(tmp_path):
    lib = make_library(tmp_path, name=".Client.md")
    data, revision = writer.read_record(lib.root, lib.path)
    assert data == ACME.encode() and revision == rev(ACME.encode())
    result = do_update(lib, revision, [op_comm_append()])
    assert result.data["path"] == "Acme Bakery/.Client.md"
    assert b"- Monthly review call" in lib.read()
    assert len(lib.history()) == 1


def test_dot_directories_and_metadata_stay_excluded(lib):
    hidden = lib.root / ".hidden"
    hidden.mkdir()
    (hidden / ".Client.md").write_bytes(ACME.encode())
    for path in (hidden / ".Client.md", lib.meta, lib.meta / "library.yaml"):
        with pytest.raises(EngagementsError) as exc:
            writer.read_record(lib.root, path)
        assert (exc.value.code, exc.value.details["reason"]) == ("not_found", "excluded"), path


def test_non_md_file_is_not_a_record(lib):
    other = lib.path.with_name("Client.txt")
    other.write_bytes(ACME.encode())
    with pytest.raises(EngagementsError) as exc:
        writer.read_record(lib.root, other)
    assert (exc.value.code, exc.value.details["reason"]) == ("not_found", "not_a_record")

