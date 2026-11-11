import multiprocessing
import threading
import time

import pytest
from engagements.errors import EngagementsError, LockTimeout
from engagements.fsutil import FileLock, atomic_write, normalize_text, rev, section_key


def test_revision_and_normalization():
    assert rev(b"abc") == "ba7816bf8f01cfea"
    assert normalize_text("  Ｃafé—BAKERY!  ") == "café bakery"
    assert section_key("  Ｃafé—BAKERY!  ") == "café—bakery!"


def test_atomic_write_replaces(tmp_path):
    path = tmp_path / "record.md"
    path.write_bytes(b"old")
    atomic_write(path, b"new")
    assert path.read_bytes() == b"new"
    assert list(tmp_path.iterdir()) == [path]


def test_io_errors_are_wrapped(tmp_path):
    with pytest.raises(EngagementsError) as exc:
        atomic_write(tmp_path / "missing" / "file", b"x")
    assert exc.value.code == "io_error"


def test_atomic_failure_cleans_temp_and_preserves_target(tmp_path, monkeypatch):
    path = tmp_path / 'record.md'
    path.write_bytes(b'old')
    def fail(*args):
        raise OSError('failure')
    monkeypatch.setattr('engagements.fsutil.os.replace', fail)
    with pytest.raises(EngagementsError) as exc:
        atomic_write(path, b'new')
    assert exc.value.code == 'io_error'
    assert path.read_bytes() == b'old'
    assert list(tmp_path.iterdir()) == [path]


def test_atomic_directory_fsync_unsupported(tmp_path, monkeypatch):
    import errno
    import os
    import stat
    original = os.fsync
    def fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, 'unsupported')
        original(fd)
    monkeypatch.setattr('engagements.fsutil.os.fsync', fsync)
    path = tmp_path / 'record.md'
    atomic_write(path, b'new')
    assert path.read_bytes() == b'new'


def test_a_failed_directory_sync_after_the_rename_is_returned_not_raised(tmp_path, monkeypatch):
    import errno
    import os
    import stat
    if os.name == "nt":
        pytest.skip("Windows has no directory sync: the helper skips that step there")
    original = os.fsync
    def fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, 'injected directory sync failure')
        original(fd)
    monkeypatch.setattr('engagements.fsutil.os.fsync', fsync)
    path = tmp_path / 'record.md'
    path.write_bytes(b'old')
    failure = atomic_write(path, b'new')
    assert isinstance(failure, OSError) and failure.errno == errno.EIO
    assert path.read_bytes() == b'new'


def _wait_for_release(path, ready, acquired):
    ready.set()
    with FileLock(path, timeout=3):
        acquired.set()


def test_process_waiter_enters_after_release(tmp_path):
    path = tmp_path / 'record.lock'
    ctx = multiprocessing.get_context('spawn')
    ready, acquired = ctx.Event(), ctx.Event()
    with FileLock(path):
        p = ctx.Process(target=_wait_for_release, args=(path, ready, acquired))
        p.start()
        assert ready.wait(3)
        assert not acquired.wait(0.2)
    assert acquired.wait(3)
    p.join(3)
    assert p.exitcode == 0


def _contend(path, queue):
    try:
        with FileLock(path, timeout=0.2):
            queue.put("acquired")
    except LockTimeout:
        queue.put("timeout")


def test_lock_excludes_thread_and_process(tmp_path):
    path = tmp_path / "record.lock"
    ctx = multiprocessing.get_context("spawn")
    q = ctx.Queue()
    outcome = []
    def thread():
        try:
            with FileLock(path, timeout=0.2):
                outcome.append("acquired")
        except LockTimeout:
            outcome.append("timeout")
    with FileLock(path):
        t = threading.Thread(target=thread)
        t.start()
        p = ctx.Process(target=_contend, args=(path, q))
        p.start()
        t.join(5)
        p.join(10)
        assert not t.is_alive()
        assert p.exitcode == 0
        assert outcome == ["timeout"]
        assert q.get(timeout=1) == "timeout"
    with FileLock(path, timeout=0.2):
        pass


def test_lock_waits_for_release(tmp_path):
    path = tmp_path / "record.lock"
    acquired = threading.Event()
    def contender():
        with FileLock(path, timeout=2):
            acquired.set()
    with FileLock(path):
        t = threading.Thread(target=contender)
        t.start()
        time.sleep(0.1)
        assert not acquired.is_set()
    t.join(3)
    assert acquired.is_set()
