"""Ownership of Windows handles on error paths, and the held-chain lifecycle.

``_WinDir.open_dir``, ``mkdir`` and ``open`` acquire a native handle, check it,
and re-check the parent directory before handing the handle to the caller. Until
that hand-off the method owns the handle, so a failure in any check, the final
parent check included, must close it exactly once. These tests run everywhere
against a protocol fake of ``_winfs`` and, on Windows, against the real backend
wrapped in the same spy, so the native handles are the ones counted.
"""

import errno
import os
import posixpath
import sys
from types import SimpleNamespace

import pytest
from engagements import fsutil

BACKENDS = ["fake", pytest.param("native", marks=pytest.mark.skipif(sys.platform != "win32",
                                                                    reason="native Windows backend"))]


class FakeWinfs:
    """Just enough of ``_winfs`` for ``_WinDir``: integer handles mapped to slash paths."""

    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000
    FILE_SHARE_READ = 1
    FILE_SHARE_WRITE = 2
    FILE_SHARE_DELETE = 4
    FILE_READ_ATTRIBUTES = 0x80
    FILE_WRITE_ATTRIBUTES = 0x100

    def __init__(self):
        self.paths = {}
        self.kinds = {}
        self.open_handles = set()
        self.deleted = []
        self._next = 100

    def _new(self, path, kind):
        self._next += 1
        self.paths[self._next] = path
        self.kinds[self._next] = kind
        self.open_handles.add(self._next)
        return self._next

    def root(self, path="/lib"):
        return self._new(path, "dir")

    def open_child_directory(self, parent, name, *, writable=True, create=False, open_if=False, filename=None):
        return self._new(posixpath.join(self.paths[parent], name), "dir")

    def open_child_file(self, parent, name, access, share, *, create=False, open_if=False, filename=None):
        return self._new(posixpath.join(self.paths[parent], name), "file")

    def open_child_for_delete(self, parent, name, filename=None):
        return self._new(posixpath.join(self.paths[parent], name), "dir")

    def delete_on_close(self, handle, name):
        self.deleted.append(self.paths[handle])

    def file_info(self, handle):
        if handle not in self.open_handles:
            raise OSError(errno.EBADF, "closed handle")
        return SimpleNamespace(is_reparse_point=False, is_directory=self.kinds[handle] == "dir", volume=1)

    def final_path(self, handle):
        return self.paths[handle]

    @staticmethod
    def parent_of(path):
        return posixpath.dirname(path)

    @staticmethod
    def same_path(a, b):
        return a == b

    def duplicate(self, handle):
        return self._new(self.paths[handle], self.kinds[handle])

    def close(self, handle):
        if handle not in self.open_handles:
            raise AssertionError(f"handle {handle} closed twice")
        self.open_handles.discard(handle)

    def to_fd(self, handle, flags):
        return 1000 + handle


class Spy:
    """Wraps a backend: records acquisitions and closes, and fails the Nth parent attribute query."""

    def __init__(self, base, parent_handle, fail_on_parent_call=None, failure="oserror"):
        self.base = base
        self.parent_handle = parent_handle
        self.fail_on = fail_on_parent_call
        self.failure = failure
        self.parent_calls = 0
        self.sequence = []
        self.acquired = []
        self.closed = []
        self.events = []

    def __getattr__(self, name):
        return getattr(self.base, name)

    def file_info(self, handle):
        if handle == self.parent_handle:
            self.parent_calls += 1
            self.sequence.append("parent")
            if self.parent_calls == self.fail_on:
                if self.failure == "oserror":
                    raise OSError(errno.EIO, "injected attribute query failure")
                return SimpleNamespace(is_reparse_point=True, is_directory=True,
                                       volume=self.base.file_info(handle).volume)
        else:
            self.sequence.append("child")
        return self.base.file_info(handle)

    def _acquire(self, handle):
        self.acquired.append(handle)
        self.events.append(("acquire", handle))
        return handle

    def open_child_for_delete(self, *args, **kwargs):
        # The mkdir cleanup's own handle. Windows reuses handle values, so closes are
        # attributed to an acquisition by order, not by value alone.
        handle = self.base.open_child_for_delete(*args, **kwargs)
        self.events.append(("cleanup", handle))
        return handle

    def closes_of_acquired(self, handle):
        """Closes of ``handle`` after its acquisition and before that value is handed out again."""
        start = self.events.index(("acquire", handle)) + 1
        count = 0
        for kind, value in self.events[start:]:
            if value != handle:
                continue
            if kind == "close":
                count += 1
            else:
                break
        return count

    def open_child_directory(self, *args, **kwargs):
        return self._acquire(self.base.open_child_directory(*args, **kwargs))

    def open_child_file(self, *args, **kwargs):
        return self._acquire(self.base.open_child_file(*args, **kwargs))

    def close(self, handle):
        self.closed.append(handle)
        self.events.append(("close", handle))
        self.base.close(handle)


@pytest.fixture
def held(monkeypatch, tmp_path, request):
    """``(root, base_backend, root_path)`` for the requested backend; the root is closed afterwards."""
    monkeypatch.setattr(fsutil, "_HANDLES", True)
    monkeypatch.setattr(os, "O_BINARY", getattr(os, "O_BINARY", 0), raising=False)
    if request.param == "fake":
        base = FakeWinfs()
        monkeypatch.setattr(fsutil, "_winfs", base)
        root = fsutil._WinDir(base.root(), "/lib", "", 1, True)
        root_path = None
    else:
        from engagements import _winfs as base

        root_path = tmp_path / "lib"
        root_path.mkdir()
        (root_path / "c").mkdir()
        (root_path / "f.md").write_bytes(b"x")
        root = fsutil._WinDir.open_root(root_path)
    yield root, base, root_path
    monkeypatch.setattr(fsutil, "_winfs", base)
    root.close()


def call(root, method):
    if method == "open_dir":
        return root.open_dir("c")
    if method == "mkdir":
        return root.mkdir("m")
    return root.open("f.md", os.O_RDONLY)


@pytest.mark.parametrize("held", BACKENDS, indirect=True)
@pytest.mark.parametrize("method", ["open_dir", "mkdir", "open"])
@pytest.mark.parametrize("failure", ["oserror", "refusal"])
def test_a_failed_final_parent_check_closes_the_acquired_handle_exactly_once(held, monkeypatch, method, failure):
    root, base, root_path = held
    spy = Spy(base, root.handle, fail_on_parent_call=2, failure=failure)
    monkeypatch.setattr(fsutil, "_winfs", spy)
    expected = OSError if failure == "oserror" else fsutil._NotRealDirectory
    with pytest.raises(expected):
        call(root, method)
    # The final parent check is the one that failed: parent, child, parent.
    assert spy.sequence[:3] == ["parent", "child", "parent"], spy.sequence
    assert len(spy.acquired) == 1
    child = spy.acquired[0]
    assert spy.closes_of_acquired(child) == 1, (spy.events, child)
    assert root.handle not in spy.closed
    if isinstance(base, FakeWinfs):
        assert child not in base.open_handles
        if method == "mkdir":
            assert base.deleted == ["/lib/m"]  # the directory this call created is removed
    else:
        monkeypatch.setattr(fsutil, "_winfs", base)
        if method == "open_dir":
            # A leaked directory hold would deny delete sharing and block this rename.
            os.rename(root_path / "c", root_path / "c2")
        elif method == "mkdir":
            assert not (root_path / "m").exists()
        else:
            os.rename(root_path / "f.md", root_path / "g.md")


@pytest.mark.parametrize("held", BACKENDS, indirect=True)
@pytest.mark.parametrize("method", ["open_dir", "mkdir", "open"])
def test_a_successful_call_transfers_ownership_and_closes_nothing(held, monkeypatch, method):
    root, base, _ = held
    spy = Spy(base, root.handle)
    monkeypatch.setattr(fsutil, "_winfs", spy)
    result = call(root, method)
    try:
        assert len(spy.acquired) == 1
        assert spy.acquired[0] not in spy.closed
    finally:
        monkeypatch.setattr(fsutil, "_winfs", base)
        if method == "open":
            if not isinstance(base, FakeWinfs):
                os.close(result)
        else:
            result.close()


@pytest.fixture
def fake(monkeypatch):
    base = FakeWinfs()
    monkeypatch.setattr(fsutil, "_HANDLES", True)
    monkeypatch.setattr(fsutil, "_winfs", base)
    return base


def test_a_closed_intermediate_keeps_the_chain_above_a_held_leaf(fake):
    root = fsutil._WinDir(fake.root(), "/lib", "", 1, True)
    middle = root.open_dir("a")
    leaf = middle.open_dir("b")
    root.close()
    middle.close()
    # Both are logically closed, but the leaf is still held, so every handle above it is too.
    assert {root.handle, middle.handle} <= fake.open_handles
    assert root.handle is not None and middle.handle is not None
    leaf.close()
    assert fake.open_handles == set()


def test_releasing_a_child_does_not_release_a_parent_that_is_still_open(fake):
    root = fsutil._WinDir(fake.root(), "/lib", "", 1, True)
    child = root.open_dir("a")
    child.close()
    assert root.handle in fake.open_handles
    root.close()
    assert fake.open_handles == set()


def test_a_duplicate_below_the_root_holds_the_same_parent_chain(fake):
    root = fsutil._WinDir(fake.root(), "/lib", "", 1, True)
    middle = root.open_dir("a")
    copy = middle.dup()
    root.close()
    middle.close()
    assert root.handle in fake.open_handles  # the duplicate still holds the root
    copy.close()
    assert fake.open_handles == set()


def test_a_duplicate_of_the_root_is_independent(fake):
    root = fsutil._WinDir(fake.root(), "/lib", "", 1, True)
    copy = root.dup()
    copy.close()
    assert root.handle in fake.open_handles
    root.close()
    assert fake.open_handles == set()


def test_open_root_forwards_writable_to_the_windows_backend(monkeypatch):
    seen = {}

    def fake_open_root(root, identity=None, writable=True):
        seen["writable"] = writable
        return "held"

    monkeypatch.setattr(fsutil, "_HANDLES", True)
    monkeypatch.setattr(fsutil._WinDir, "open_root", staticmethod(fake_open_root))
    assert fsutil._Dir.open_root("/lib", writable=False) == "held"
    assert seen == {"writable": False}


@pytest.mark.parametrize("name", ["CONIN$", "conout$", "CONOUT$.md", "Conin$.txt"])
def test_console_device_names_are_refused_before_the_nt_call(name):
    with pytest.raises(OSError) as raised:
        fsutil._check_leaf(name)
    assert raised.value.errno == errno.EINVAL


@pytest.mark.skipif(not (fsutil._RELATIVE and fsutil._NOFOLLOW) or sys.platform == "win32",
                    reason="descriptor-relative POSIX backend")
def test_a_posix_mkdir_whose_open_fails_removes_the_directory_it_created(tmp_path, monkeypatch):
    root = fsutil._Dir.open_root(tmp_path)
    try:
        def failing_open_dir(self, name, *args, **kwargs):
            raise OSError(errno.EMFILE, "injected open failure", name)

        monkeypatch.setattr(fsutil._Dir, "open_dir", failing_open_dir)
        with pytest.raises(OSError):
            root.mkdir("new")
        assert not (tmp_path / "new").exists()
    finally:
        root.close()
