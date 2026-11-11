"""Housekeeping holds its limits: snapshot size, history retention after a failed
attempt, temp files an interrupted write leaves behind, and the warnings a
published-but-unchecked metadata entry produces."""
import contextlib
import errno
import os
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from engagements import doctor, fsutil, library, writer
from engagements.errors import EngagementsError
from engagements.fsutil import rev
from writer_support import ACME, WEBSITE, fixed_clock, op_status, ticking_clock


def _patch_method(monkeypatch, name, wrapper):
    """Wrap ``name`` on every held-directory class this platform can use."""
    classes = [fsutil._Dir]
    if hasattr(fsutil, "_WinDir"):
        classes.append(fsutil._WinDir)
    for cls in classes:
        original = getattr(cls, name)

        def replacement(self, *args, _original=original, **kwargs):
            return wrapper(self, _original, *args, **kwargs)

        monkeypatch.setattr(cls, name, replacement)


def _snapshot(lib, data):
    """Write ``data`` as a snapshot under its own revision and return that revision."""
    directory = lib.meta / "snapshots"
    directory.mkdir(parents=True, exist_ok=True)
    revision = rev(data)
    (directory / f"{revision}.md").write_bytes(data)
    return revision


def _temp(lib, data=b"leftover", *, prefix="", leaf=None):
    """A temp file the plugin's writer would have left behind, in the record's folder."""
    name = leaf or f".engagements-{prefix or 'a' * 16}.tmp"
    path = lib.path.parent / name
    path.write_bytes(data)
    return path


def _old(lib, path, *, age=timedelta(hours=2)):
    stamp = (fixed_clock() - age).timestamp()
    os.utime(path, (stamp, stamp))
    return path


# --- 1. snapshot reads are bounded by the record size limit ----------------------


def test_a_snapshot_larger_than_a_record_is_never_loaded(lib, monkeypatch):
    oversized = ACME.encode() + b"x" * writer.MAX_RECORD_BYTES
    revision = _snapshot(lib, oversized)
    reads = []
    real_read = os.read

    def counted(fd, size):
        reads.append(size)
        return real_read(fd, size)

    monkeypatch.setattr(writer.os, "read", counted)
    assert writer.load_snapshot(lib.root, revision) is None
    assert reads == []  # refused from the size on the descriptor, not read into memory


@pytest.mark.skipif(os.name == "nt", reason="a sparse file needs a POSIX filesystem here")
def test_a_sparse_snapshot_is_refused_from_its_size_alone(lib, monkeypatch):
    directory = lib.meta / "snapshots"
    directory.mkdir()
    giant = directory / ("d" * 16 + ".md")
    with giant.open("wb") as handle:
        handle.truncate(64 * 1024 * 1024)  # sparse: no block of it is written
    reads = []
    real_read = os.read

    def counted(fd, size):
        reads.append(size)
        return real_read(fd, size)

    monkeypatch.setattr(writer.os, "read", counted)
    assert writer.load_snapshot(lib.root, "d" * 16) is None
    assert reads == []
    assert giant.stat().st_size == 64 * 1024 * 1024


def test_a_flipped_stale_update_says_the_snapshot_is_too_large_to_use(lib):
    data = lib.read()
    base = _snapshot(lib, data)
    (lib.meta / "snapshots" / f"{base}.md").write_bytes(data + b"x" * writer.MAX_RECORD_BYTES)
    lib.path.write_bytes(data + b"\nexternal edit\n")
    with pytest.raises(EngagementsError) as exc:
        writer.update(lib.root, lib.path, base, "owner email", [op_status()], kind="client", profile="studio-dev",
                      clock=fixed_clock, client_exists=lambda value: True)
    assert (exc.value.code, exc.value.details["reason"]) == ("conflict", "base_unavailable")
    assert "larger than a record" in exc.value.message


@pytest.mark.parametrize("poison", ["wrong bytes", "too large"])
def test_a_read_replaces_a_snapshot_it_could_not_use(lib, poison):
    data = lib.read()
    revision = rev(data)
    directory = lib.meta / "snapshots"
    directory.mkdir(parents=True, exist_ok=True)
    snapshot = directory / f"{revision}.md"
    snapshot.write_bytes(b"not the record's bytes" if poison == "wrong bytes"
                         else data + b"x" * writer.MAX_RECORD_BYTES)
    writer.read_record(lib.root, lib.path)
    assert snapshot.read_bytes() == data


# --- 2. history retention holds for a failed attempt that wrote history ---------


def test_the_history_folder_stays_within_its_cap_after_failed_updates(lib):
    clock = ticking_clock()
    failures = 0
    for _ in range(55):
        before = lib.read()

        def editor(before=before):
            lib.path.write_bytes(before + b"\nexternal edit\n")

        with pytest.raises(EngagementsError) as exc:
            writer.update(lib.root, lib.path, rev(before), "owner email", [op_status()], kind="client",
                          profile="studio-dev", clock=clock, client_exists=lambda value: True,
                          before_final_rehash=editor)
        assert exc.value.details["reason"] == "changed_during_write"
        failures += 1
    assert failures == 55
    assert len(lib.history()) == writer.HISTORY_KEEP


def test_the_history_folder_stays_within_its_cap_after_successful_updates(lib):
    clock = ticking_clock()
    revision = rev(lib.read())
    for index in range(55):
        result = writer.update(lib.root, lib.path, revision, "owner email", [op_status("paused" if index % 2 else "active")],
                               kind="client", profile="studio-dev", clock=clock, client_exists=lambda value: True)
        revision = result.data["new_rev"]
    assert len(lib.history()) == writer.HISTORY_KEEP


# --- 3. temp files a crash left behind are reported, never removed -------------


def _do_update(lib, path=None, kind="client"):
    path = path or lib.path
    ops = [op_status()] if kind == "client" else [{"op": "append_to_section", "section": "Notes", "text": "- reviewed"}]
    return writer.update(lib.root, path, rev(path.read_bytes()), "owner email", ops, kind=kind,
                         profile="studio-dev", clock=fixed_clock, client_exists=lambda value: True)


def test_an_update_leaves_an_interrupted_write_s_temp_file_alone(lib):
    stale = _old(lib, _temp(lib, prefix="a" * 16))
    result = _do_update(lib)
    assert result.warnings == []
    assert stale.exists()
    assert [item.path for item in doctor.run_doctor(lib.root).findings if item.code == "stale_temp"] == [
        f"Acme Bakery/{stale.name}"]


def test_no_write_path_unlinks_a_temp_file(lib, monkeypatch):
    stale = _old(lib, _temp(lib, prefix="a" * 16))
    removed = []

    def note(self, original, name):
        if name.endswith(".tmp"):
            removed.append(name)
        return original(self, name)

    _patch_method(monkeypatch, "unlink", note)
    result = _do_update(lib)
    assert removed == []
    assert stale.exists()
    assert result.warnings == []


@contextlib.contextmanager
def _paused_writer(monkeypatch, operation, is_own_temp):
    """Start ``operation`` on a thread and stop it inside ``os.fsync`` with its real temp file open.

    The temp file is dated two hours back while it is paused, which models a slow
    or stalled writer: the file is old enough for any age rule to look stale, and
    the descriptor is genuinely open. Yields the recorded temp path.
    """
    ready, resume = threading.Event(), threading.Event()
    state = {}
    real_fsync = os.fsync

    def fsync(fd):
        target = Path(os.readlink(f"/proc/self/fd/{fd}"))
        if threading.current_thread().name == "paused-writer" and is_own_temp(target) and not ready.is_set():
            old = (fixed_clock() - timedelta(hours=2)).timestamp()
            os.utime(fd, (old, old))
            state.update(temp=target, fd=fd)
            ready.set()
            if not resume.wait(20):
                raise RuntimeError("the paused writer was never resumed")
        return real_fsync(fd)

    def worker():
        try:
            state["result"] = operation()
        except BaseException as exc:  # noqa: BLE001 - the failure is the assertion
            state["error"] = exc

    monkeypatch.setattr(os, "fsync", fsync)
    thread = threading.Thread(target=worker, name="paused-writer", daemon=True)
    thread.start()
    try:
        assert ready.wait(20), "the writer never reached its temp file"
        yield state
    finally:
        resume.set()
        thread.join(20)
    assert not thread.is_alive()
    assert "error" not in state, state.get("error")


def _live_producers(lib, projects, target):
    """The three live writers a sweep has to leave alone, each with the shape of its own temp file."""
    opened = library.open_library(lib.root, create_default=False)

    def update():
        return _do_update(lib, target, "project")

    def create():
        return opened.create("client", "New Customer", profile="studio-dev", today=fixed_clock)

    def read():
        return writer.read_record(lib.root, lib.path, clock=fixed_clock)

    def in_the_shared_folder(path):
        return path.parent == projects and path.name.startswith(".engagements-")

    def a_first_publish(path):
        return path.name.startswith(".engagements-new-") and path.name.endswith(".tmp")

    def a_snapshot(path):
        return path.parent.name == "snapshots" and path.name.endswith(".tmp")

    return {"update": (update, in_the_shared_folder), "create": (create, a_first_publish), "read": (read, a_snapshot)}


@pytest.mark.skipif(os.name == "nt", reason="the pause reads the open descriptor through /proc")
@pytest.mark.parametrize("producer", ["update", "create", "read"])
def test_a_live_open_temp_file_survives_a_concurrent_update(lib, monkeypatch, producer):
    projects = lib.path.parent / "Projects"
    projects.mkdir()
    (projects / "Alpha.md").write_text(WEBSITE.replace("acme-website-2026", "project-alpha"))
    target = projects / "Beta.md"
    target.write_text(WEBSITE.replace("acme-website-2026", "project-beta"))
    operation, is_own_temp = _live_producers(lib, projects, target)[producer]

    with _paused_writer(monkeypatch, operation, is_own_temp) as state:
        assert os.fstat(state["fd"]).st_nlink == 1  # the paused writer still holds its only name
        _do_update(lib)  # an ordinary successful update, which used to sweep the whole root
        assert os.fstat(state["fd"]).st_nlink == 1
        assert state["temp"].exists()
    assert "error" not in state
    if producer == "update":
        assert "- reviewed" in target.read_text()
    elif producer == "create":
        assert (lib.root / "New Customer" / "Client.md").exists()


def test_doctor_reports_a_stale_temp_file_with_its_age_and_removes_nothing(lib):
    stale = lib.path.parent / f".engagements-{'a' * 16}.tmp"
    stale.write_bytes(b"leftover")
    stamp = (datetime.now(UTC) - timedelta(days=2)).timestamp()
    os.utime(stale, (stamp, stamp))
    report = doctor.run_doctor(lib.root)
    [finding] = [item for item in report.findings if item.code == "stale_temp"]
    assert finding.severity == "warning"
    assert finding.path == f"Acme Bakery/{stale.name}"
    assert "2 days" in finding.message
    assert stale.exists()


def test_doctor_leaves_a_recent_temp_file_unreported(lib):
    recent = lib.path.parent / f".engagements-{'b' * 16}.tmp"
    recent.write_bytes(b"a write in flight")
    assert "stale_temp" not in {item.code for item in doctor.run_doctor(lib.root).findings}


def test_a_temp_file_outside_the_plugin_s_own_names_is_not_reported(lib):
    lookalike = lib.path.parent / ".engagements-not-a-temp.tmp"
    lookalike.write_bytes(b"not ours")
    _old(lib, lookalike, age=timedelta(days=3))
    assert "stale_temp" not in {item.code for item in doctor.run_doctor(lib.root).findings}
    assert lookalike.exists()


# --- 4. a published entry whose re-check failed is a warning, never an error ----


def _replace_then_fail(monkeypatch, where):
    """Simulate the Windows backend: the rename publishes the entry, then a check on it fails."""
    failed = []

    def replace(self, original, source, target):
        result = original(self, source, target)
        if self.relpath == where and target.endswith(".md"):
            failed.append(target)
            return OSError(errno.EIO, "the entry could not be re-checked after the rename")
        return result

    _patch_method(monkeypatch, "replace", replace)
    return failed


def test_a_history_copy_that_could_not_be_re_checked_is_a_warning(lib, monkeypatch):
    failed = _replace_then_fail(monkeypatch, ".engagements/history/acme-bakery")
    result = _do_update(lib)
    assert failed  # the copy is written; only the check on it failed
    assert [w["code"] for w in result.warnings] == ["maintenance_warning"]
    assert result.data["new_rev"]
    assert len(lib.history()) == 1


def test_a_committed_snapshot_that_could_not_be_re_checked_is_a_warning(lib, monkeypatch):
    failed = _replace_then_fail(monkeypatch, ".engagements/snapshots")
    result = _do_update(lib)
    assert failed
    assert [w["code"] for w in result.warnings] == ["maintenance_warning"]
    assert result.data["new_rev"]


def test_a_read_snapshot_that_could_not_be_re_checked_is_a_warning(lib, monkeypatch):
    failed = _replace_then_fail(monkeypatch, ".engagements/snapshots")
    warnings = []
    data, revision = writer.read_record(lib.root, lib.path, clock=fixed_clock, warnings=warnings)
    assert data == lib.read()
    assert failed
    assert [w["code"] for w in warnings] == ["maintenance_warning"]
    assert writer.load_snapshot(lib.root, revision) == data
