"""Filesystem primitives independent of the Hermes runtime."""

import errno
import hashlib
import os
import stat
import sys
import tempfile
import time
import unicodedata
from contextlib import contextmanager, suppress
from pathlib import Path

from .errors import EngagementsError, LockTimeout

if os.name == "nt":  # pragma: no cover - exercised on Windows
    import msvcrt
else:
    import fcntl

try:
    if sys.platform != "win32":
        raise ImportError("handle-confined writes are the Windows backend")
    from . import _winfs
except (ImportError, OSError, AttributeError):  # not Windows, or kernel32 is unusable
    _winfs = None


def rev(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def normalize_text(s: str) -> str:
    normalized = unicodedata.normalize("NFKC", s).casefold()
    return " ".join("".join(" " if unicodedata.category(c)[0] in "PS" else c for c in normalized).split())


def section_key(s: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", s).casefold().split())


# --- descriptor-relative filesystem access ----------------------------------------
#
# Every plugin write below the root goes through a directory held open by
# descriptor. Each component is opened with O_NOFOLLOW relative to its verified
# parent, and files are created, linked, renamed and removed relative to that
# descriptor, so swapping a checked directory for a symlink between the check and
# the write cannot redirect the write outside the library.
#
# Without dir_fd or O_NOFOLLOW there is no equivalent guarantee: a path-based
# write follows a directory swapped for a link, and a lock open without
# O_NOFOLLOW creates a planted link's outside target. Such a platform gets no
# writes at all; only reads fall back to paths.
#
# Windows has neither primitive, so it has its own mechanism (``_WinDir``,
# built on ``_winfs``): every directory of the chain is held by handle, opened
# without following a reparse point and without delete sharing, and every child
# open, create, replace, hard link and delete is made relative to that handle
# (``OBJECT_ATTRIBUTES.RootDirectory``), never through a path rebuilt from it.
# That is what confines the write: a reparse point set on the held directory
# cannot redirect a relative operation, the kernel refuses to resolve the name
# (WinError 1921) and nothing lands outside (native tests). Delete sharing is
# withheld so the directory itself cannot be renamed away, while write sharing
# is granted because the file system opens the directory for write itself when
# a link or a rename adds a name to it.
# Only a platform with neither mechanism fails closed.

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_DIR_OPEN = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _NOFOLLOW | _CLOEXEC
_RELATIVE = all(f in os.supports_dir_fd for f in (os.open, os.mkdir, os.rmdir, os.unlink, os.link, os.rename)) \
    and os.listdir in os.supports_fd and os.scandir in os.supports_fd and hasattr(os, "O_DIRECTORY")
_HANDLES = _winfs is not None  # Windows: handle-confined access (``_WinDir``)
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
UNSUPPORTED_PLATFORM = "unsupported_platform"
ROOT_CHANGED = "root_changed"


def _safe_writes():
    """True when writes can be confined to held directories without following links.

    Either descriptor-relative calls with ``O_NOFOLLOW`` (POSIX) or held,
    no-follow handles (Windows). Read at call time so tests can simulate a
    platform without these primitives.
    """
    return bool(_HANDLES or (_RELATIVE and _NOFOLLOW))


class _Refusal(Exception):
    """A filesystem step was refused before it ran. Maps to ``io_error`` with ``reason``."""

    def __init__(self, relpath, reason):
        super().__init__(relpath)
        self.relpath = relpath
        self.reason = reason


class _NotRealDirectory(_Refusal):
    """A path component that should be a real directory is a symlink or another file type."""


class _UnsupportedPlatform(_Refusal):
    """The platform lacks the primitives that keep a write inside the library."""

    def __init__(self, relpath=""):
        super().__init__(relpath, UNSUPPORTED_PLATFORM)


def _require_safe_writes(relpath=""):
    if not _safe_writes():
        raise _UnsupportedPlatform(relpath)


def root_identity(info):
    """The identity of a directory, from its ``stat`` result, for detecting a replaced root."""
    return (info.st_dev, info.st_ino)


@contextmanager
def _located(directory, *names):
    """Give an OSError from a descriptor-relative call its full path, not just the leaf name."""
    try:
        yield
    except OSError as exc:
        if names and exc.filename == names[0]:
            exc.filename = directory._at(names[0])
        if len(names) > 1 and exc.filename2 == names[1]:
            exc.filename2 = directory._at(names[1])
        raise


# Test seam (TOOLS.md "Test hooks for implementers"): called with the held directory
# and the child name immediately before a mutating child operation, on either
# platform. Production code never sets it. The native Windows tests use it to change
# reparse data on a directory the plugin is holding, in the middle of an operation,
# which is the one thing a test cannot inject from outside the hold.
_before_child_mutation = None


def _lexical_real_directory(path):
    """True when ``path`` is a directory and not a symlink; None when absent."""
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return None
    return stat.S_ISDIR(mode)


class _Dir:
    """A directory held open by descriptor; every operation is relative to it.

    ``relpath`` is its slash path from the library root, for error details.
    Where the platform has neither ``dir_fd`` support nor the Windows handle
    backend, only the read operations degrade to path-based calls; every
    mutation is refused with ``_UnsupportedPlatform`` unless ``_safe_writes()``
    holds. On Windows ``open_root`` returns a ``_WinDir`` with the same surface.
    """

    def __init__(self, fd, path, relpath):
        self.fd = fd
        self.path = path
        self.relpath = relpath

    @classmethod
    def open_root(cls, root, identity=None, writable=True):
        """Open the library root without following a symlink at its last component.

        With ``identity`` (from ``root_identity`` when the library was opened), a
        root that is now a different directory, for instance because an ancestor
        was replaced, is refused with reason ``root_changed``. ``writable`` is only
        meaningful on Windows, where a write-capable handle is what keeps the root
        from being renamed or turned into a reparse point for the operation.

        The read-only path fallback makes the same static type and identity
        checks. Without O_NOFOLLOW it also uses path-only child
        checks, rather than opening directory links through an unguarded fd.
        This does not make unsupported platforms race-safe.
        """
        if _HANDLES:
            return _WinDir.open_root(root, identity, writable)
        if not _RELATIVE or not _NOFOLLOW:  # read-only fallback
            info = os.lstat(root)
            if not stat.S_ISDIR(info.st_mode):
                raise _NotRealDirectory("", ROOT_CHANGED if identity is not None else "not_a_directory")
            held = cls(None, os.fspath(root), "")
            if identity is not None and held.identity() != tuple(identity):
                raise _NotRealDirectory("", ROOT_CHANGED)
            return held
        try:
            fd = os.open(root, _DIR_OPEN)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                raise _NotRealDirectory("", ROOT_CHANGED if identity is not None else "not_a_directory") from None
            raise
        held = cls(fd, os.fspath(root), "")
        try:
            if identity is not None and held.identity() != tuple(identity):
                raise _NotRealDirectory("", ROOT_CHANGED)
        except BaseException:
            held.close()
            raise
        return held

    def identity(self):
        info = os.stat(self.path) if self.fd is None else os.fstat(self.fd)
        return root_identity(info)

    def dup(self):
        """An independently closable handle on the same directory."""
        return _Dir(None if self.fd is None else os.dup(self.fd), self.path, self.relpath)

    def _child_rel(self, name):
        return f"{self.relpath}/{name}" if self.relpath else name

    def _at(self, name):
        return os.path.join(self.path, name)

    def open_dir(self, name, writable=None):
        """Open a child directory without following a symlink. None when absent.

        ``writable`` is only meaningful on Windows (see ``_WinDir.open_dir``); the
        descriptor backend ignores it.
        """
        rel = self._child_rel(name)
        if self.fd is None:  # pragma: no cover
            state = _lexical_real_directory(self._at(name))
            if state is None:
                return None
            if not state:
                raise _NotRealDirectory(rel, "not_a_directory")
            return _Dir(None, self._at(name), rel)
        try:
            with _located(self, name):
                fd = os.open(name, _DIR_OPEN, dir_fd=self.fd)
        except FileNotFoundError:
            return None
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                raise _NotRealDirectory(rel, "not_a_directory") from None
            raise
        return _Dir(fd, self._at(name), rel)

    def ensure_dir(self, name):
        """Open a child directory, creating it when absent. Never follows a symlink."""
        for _ in range(3):
            child = self.open_dir(name)
            if child is not None:
                return child
            try:
                return self.mkdir(name)
            except (FileExistsError, _NotRealDirectory):
                continue
        raise _NotRealDirectory(self._child_rel(name), "unstable")

    def _can_mutate(self):
        """True when a write through this directory is confined to it."""
        return self.fd is not None and _safe_writes()

    def _mutable(self, name):
        """Refuse a mutation unless it can be made relative to a held, no-follow object."""
        if not self._can_mutate():
            raise _UnsupportedPlatform(self._child_rel(name))

    def _mutation(self, name):
        """Check that a mutation is confinable, then let the test seam observe it.

        The seam runs after the check and before the write, which is the point a
        native test needs: the directory is already held and verified.
        """
        self._mutable(name)
        if _before_child_mutation is not None:
            _before_child_mutation(self, name)

    def mkdir(self, name):
        """Create a child directory and return it held.

        Returning the opened directory keeps the caller from creating a name and
        then resolving it again; on Windows that is one object-relative create.
        """
        self._mutation(name)
        with _located(self, name):
            os.mkdir(name, dir_fd=self.fd)
        try:
            child = self.open_dir(name)
            if child is None:
                raise _NotRealDirectory(self._child_rel(name), "unstable")
        except BaseException:
            # Nothing was handed back, so the caller cannot clean up the directory
            # this call created; remove it if it is still the empty one we made.
            with suppress(OSError):
                os.rmdir(name, dir_fd=self.fd)
            raise
        return child

    def rmdir(self, name):
        self._mutation(name)
        with _located(self, name):
            os.rmdir(name, dir_fd=self.fd)

    def listdir(self):
        try:
            return os.listdir(self.path if self.fd is None else self.fd)
        except OSError as exc:
            if exc.filename is None or isinstance(exc.filename, int):
                exc.filename = self.path
            raise

    def scandir(self):
        """Entries of this directory; their ``stat`` and type checks never follow links."""
        try:
            return os.scandir(self.path if self.fd is None else self.fd)
        except OSError as exc:
            if exc.filename is None or isinstance(exc.filename, int):
                exc.filename = self.path
            raise

    def open(self, name, flags, mode=0o666):
        """Open an entry; refuse final symlinks even in read fallback.

        Consult _NOFOLLOW at call time as well as the caller's flags: callers
        may cache their flags before a capability is disabled. When the kernel
        cannot enforce no-follow, check the lexical entry and compare it with
        the opened descriptor before any bytes are read. Native no-follow
        opens need neither extra syscall. Path-based directory races remain
        unsupported; every write still requires safe primitives.
        """
        writing = flags & _WRITE_FLAGS
        if writing:
            self._mutation(name)
        with _located(self, name):
            checked = None
            if not writing and (not _NOFOLLOW or not flags & _NOFOLLOW):
                checked = (os.lstat(self._at(name)) if self.fd is None
                           else os.stat(name, dir_fd=self.fd, follow_symlinks=False))
                if stat.S_ISLNK(checked.st_mode):
                    raise OSError(errno.ELOOP, "read path is a symlink", name)
            if self.fd is None:  # read-only fallback where dir_fd is missing
                fd = os.open(self._at(name), flags, mode)
            else:
                fd = os.open(name, flags, mode, dir_fd=self.fd)
            try:
                if checked is not None and root_identity(os.fstat(fd)) != root_identity(checked):
                    raise OSError(errno.ELOOP, "read path changed before open", name)
            except BaseException:
                os.close(fd)
                raise
            return fd

    def link(self, source, target) -> OSError | _Refusal | None:
        """``os.link`` of one entry to a new name, both inside this held directory.

        Returns None: like ``replace``, this backend has no check to run after
        the link.
        """
        self._mutation(target)
        with _located(self, source, target):
            os.link(source, target, src_dir_fd=self.fd, dst_dir_fd=self.fd, follow_symlinks=False)
        return None

    def replace(self, source, target) -> OSError | _Refusal | None:
        """``os.replace`` of one entry by another, both inside this held directory.

        Returns None: this backend has no check to run after the rename, and the
        per-rename contract is the one ``_WinDir.replace`` documents (a failure
        raised after the rename is returned, not raised).
        """
        self._mutation(target)
        with _located(self, source, target):
            os.replace(source, target, src_dir_fd=self.fd, dst_dir_fd=self.fd)
        return None

    def unlink(self, name):
        self._mutation(name)
        with _located(self, name):
            os.unlink(name, dir_fd=self.fd)

    def set_mtime(self, name, stamp):
        """Set the access and modification time of a regular file here, never through a link."""
        self._mutation(name)
        fd = self.open(name, os.O_RDONLY | _NOFOLLOW | _CLOEXEC | getattr(os, "O_NONBLOCK", 0))
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError(errno.EINVAL, "not a regular file", name)
            os.utime(fd, (stamp, stamp))
        finally:
            os.close(fd)

    def fsync(self):
        """Best-effort sync of this directory's entries, for after a write is already published.

        Every error is ignored. A sync that must succeed before a later step
        (the writer's required history copy) checks its own errors instead.
        """
        if self.fd is None:  # pragma: no cover
            return
        with suppress(OSError):
            os.fsync(self.fd)

    def close(self):
        """Release the descriptor. Never raises.

        On Linux the descriptor is released even when ``close`` reports an
        error (so it must not be retried), and a read-only directory descriptor
        holds no unwritten data, so the error says nothing actionable. Other
        platforms' EINTR semantics are not certified here. Raising it would
        turn an already committed write into a failure and strand handles
        still owned by the caller.
        """
        if self.fd is not None:
            fd, self.fd = self.fd, None
            with suppress(OSError):
                os.close(fd)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


_WIN_BAD_NAME_CHARS = frozenset('\\/:*?"<>|') | frozenset(chr(code) for code in range(32))
# Device names are commands wherever they appear, even with an extension ("CON.md"),
# and a trailing dot or space is stripped by the file system, so the name the
# caller asked for is not the name that would be created.
_WIN_RESERVED_NAMES = frozenset(
    ["con", "prn", "aux", "nul", "clock$", "conin$", "conout$"]
    + [f"com{number}" for number in "123456789¹²³"]
    + [f"lpt{number}" for number in "123456789¹²³"]
)


def _check_leaf(name):
    """Refuse anything but one plain path component.

    A separator would leave the held directory, a colon names an alternate data
    stream or a drive, and ``.``/``..`` are not entries. A device name or a
    trailing dot/space is refused because the file system rewrites it, so the
    object opened would not be the object named. Callers only pass names they
    derived or listed, so this is a guard, not a validation rule. The NT call is
    relative to the held handle, so this is checked before it, once, here.
    """
    if (not name or name in (".", "..") or any(char in _WIN_BAD_NAME_CHARS for char in name)
            or name[-1] in ". " or name.split(".")[0].rstrip(" ").casefold() in _WIN_RESERVED_NAMES):
        raise OSError(errno.EINVAL, "invalid file name", name)


class _WinDir(_Dir):
    """A directory held by NT handle: the Windows counterpart of a descriptor-held ``_Dir``.

    Every child operation is object-relative and goes through ``_winfs``:
    ``NtCreateFile`` with ``OBJECT_ATTRIBUTES.RootDirectory`` for opens and
    creates, ``FileRenameInformation``/``FileLinkInformation`` with
    ``RootDirectory`` for rename and hard link, and ``FileDispositionInformation``
    on an object-relative open for delete. A pathname is never built from a held
    handle and resolved again, so renaming, replacing or otherwise redirecting a
    directory between the check and the write cannot move the write.

    A write-capable handle asks for ``FILE_ADD_FILE``, ``FILE_ADD_SUBDIRECTORY``
    and ``FILE_DELETE_CHILD`` and withholds delete sharing, so the held
    directory itself cannot be renamed, deleted or replaced. Write sharing is
    granted: the file system opens the directory for write itself when a hard
    link or a rename adds a name to it, and withholding it fails both with
    WinError 32. What confines a write is that every child
    operation is relative to this handle, so a reparse point set on the held
    directory — which is all an attacker can do to an empty one, since name
    surrogate tags need an empty directory (WinError 145 otherwise) — cannot
    redirect it: the kernel refuses to resolve the relative name with WinError
    1921 and nothing appears outside (native tests). Each child keeps its parent
    object alive, so the whole chain from the root is held for the operation
    (SPEC 5.3). A directory that can be read but not written falls back to a
    read-only handle, which the kernel refuses to create children through.

    Reparse points are refused wherever the plugin opens one. Every child
    operation also re-checks the held directory's own attributes before it runs
    and again after it, and fails closed if the object became a reparse point,
    so the caller sees ``reparse_point`` rather than the kernel's raw refusal.
    That is defense in depth: the object-relative call is what confines it.

    ``open`` returns an ordinary binary CRT descriptor owning the file handle, so
    callers read, write, fsync and close it as on POSIX. Windows has no directory
    sync: ``fsync`` does nothing, ``replace`` flushes the parent handle after the
    rename as a best effort, and neither is crash evidence.
    """

    def __init__(self, handle, path, relpath, volume, writable=True, parent=None):
        super().__init__(None, path, relpath)
        self.handle = handle
        self._volume = volume
        self._writable = writable
        self._parent = parent
        self._children = 0
        self._closed = False
        if parent is not None:
            parent._children += 1

    @classmethod
    def open_root(cls, root, identity=None, writable=True):
        path = os.fspath(root)
        handle, writable = _winfs.open_held_directory(_winfs.extended(path), writable=writable)
        held = None
        try:
            info = _winfs.file_info(handle)
            if info.is_reparse_point or not info.is_directory:
                raise _NotRealDirectory("", ROOT_CHANGED if identity is not None else "not_a_directory")
            held = cls(handle, path, "", info.volume, writable)
            if identity is not None and held.identity() != tuple(identity):
                raise _NotRealDirectory("", ROOT_CHANGED)
        except BaseException:
            if held is not None:
                held.close()
            else:
                _winfs.close(handle)
            raise
        return held

    def identity(self):
        return _winfs.identity(self.handle)

    def dup(self):
        """An independent handle on the same directory object that holds the same parent chain.

        Like any child, the duplicate keeps its parent alive until it is released,
        so a duplicate taken below the root does not outlive the hold above it.
        """
        return _WinDir(_winfs.duplicate(self.handle), self.path, self.relpath, self._volume, self._writable,
                       parent=self._parent)

    def _can_mutate(self):
        return self.handle is not None and not self._closed and _safe_writes()

    def _mutable(self, name):
        super()._mutable(name)
        if not self._writable:
            raise OSError(errno.EACCES, "the directory is not open for writing", name)

    def _still_plain(self, rel):
        """Fail closed when the held object is no longer a plain directory.

        Defense in depth only: write sharing is granted (hard link and rename need
        it), so another process can set a reparse point on an empty held
        directory, but every child operation is relative to this handle and the
        kernel refuses to resolve through it (WinError 1921). This check turns
        that into a ``reparse_point`` refusal, and also covers a handle that was
        closed under the operation.
        """
        if self.handle is None or self._closed:
            raise _NotRealDirectory(rel, "not_a_directory")
        info = _winfs.file_info(self.handle)
        if info.is_reparse_point or not info.is_directory:
            raise _NotRealDirectory(rel, "reparse_point")

    def _inside(self, handle, info):
        """True when ``handle`` is an entry of this directory, on this volume.

        A read-only cross-check from handles, never used to build a path for a
        write: the object-relative call has already happened.
        """
        parent = _winfs.final_path(self.handle)
        return info.volume == self._volume and _winfs.same_path(
            _winfs.parent_of(_winfs.final_path(handle)), parent)

    def _verified(self, handle, name):
        """Check a freshly opened entry: not a reparse point, and inside this directory."""
        info = _winfs.file_info(handle)
        if info.is_reparse_point:
            raise _NotRealDirectory(self._child_rel(name), "not_a_directory")
        if not self._inside(handle, info):
            raise _NotRealDirectory(self._child_rel(name), "unstable")
        return info

    def open_dir(self, name, writable=None):
        """Open a child directory, holding it; None when absent. Reparse points are refused.

        A write-capable parent tries for a write-capable child; a directory the
        user may read but not write falls back to a read-only handle, so reads
        still work and every mutation through it is refused by the kernel.
        """
        rel = self._child_rel(name)
        self._still_plain(rel)
        _check_leaf(name)
        wanted = self._writable if writable is None else writable
        with _located(self, name):
            try:
                handle = _winfs.open_child_directory(self.handle, name, writable=wanted, filename=name)
            except FileNotFoundError:
                return None
            except PermissionError:
                if not wanted:
                    raise
                handle = _winfs.open_child_directory(self.handle, name, writable=False, filename=name)
                wanted = False
        try:
            info = _winfs.file_info(handle)
            if info.is_reparse_point or not info.is_directory:
                raise _NotRealDirectory(rel, "not_a_directory")
            if not self._inside(handle, info):
                raise _NotRealDirectory(rel, "unstable")
            self._still_plain(rel)
        except BaseException:
            # Ownership passes to the returned object only once every check has
            # passed; until then this call owns the handle and must close it.
            _winfs.close(handle)
            raise
        return _WinDir(handle, self._at(name), rel, self._volume, wanted, parent=self)

    def mkdir(self, name):
        """Create a child directory and return it held, object-relative and in one step.

        There is no separate open after the create, so a name swapped into the
        window between ``mkdir`` and ``open`` cannot be adopted.
        """
        self._mutation(name)
        rel = self._child_rel(name)
        self._still_plain(rel)
        _check_leaf(name)
        with _located(self, name):
            handle = _winfs.open_child_directory(self.handle, name, writable=True, create=True, filename=name)
        try:
            info = _winfs.file_info(handle)
            if info.is_reparse_point or not info.is_directory:
                raise _NotRealDirectory(rel, "not_a_directory")
            if not self._inside(handle, info):
                raise _NotRealDirectory(rel, "unstable")
            self._still_plain(rel)
        except BaseException:
            _winfs.close(handle)
            # The caller never received the new directory, so it cannot remove it;
            # an object-relative delete of the still-empty directory is best effort.
            with suppress(OSError, _Refusal):
                self._rmdir_created(name)
            raise
        return _WinDir(handle, self._at(name), rel, self._volume, True, parent=self)

    def _rmdir_created(self, name):
        """Remove a directory this object just created, without the mutation seam or parent re-check."""
        handle = _winfs.open_child_for_delete(self.handle, name, filename=name)
        try:
            if _winfs.file_info(handle).is_directory:
                _winfs.delete_on_close(handle, name)
        finally:
            _winfs.close(handle)

    def _unlink_created(self, name):
        """Remove a file this object just created, without the mutation seam or parent re-check."""
        handle = _winfs.open_child_for_delete(self.handle, name, filename=name)
        try:
            if not _winfs.file_info(handle).is_directory:
                _winfs.delete_on_close(handle, name)
        finally:
            _winfs.close(handle)

    def rmdir(self, name):
        self._mutation(name)
        rel = self._child_rel(name)
        self._still_plain(rel)
        _check_leaf(name)
        with _located(self, name):
            handle = _winfs.open_child_for_delete(self.handle, name, filename=name)
        try:
            if not _winfs.file_info(handle).is_directory:
                raise OSError(errno.ENOTDIR, "not a directory", name)
            with _located(self, name):
                _winfs.delete_on_close(handle, name)
        finally:
            _winfs.close(handle)
        self._still_plain(rel)

    def _list(self, call):
        """List entries by pathname. Never feeds a write: names only select a name.

        Enumeration is the one operation that stays path-based. Every name it
        returns is used for an exclusive, object-relative create or a
        no-follow object-relative open, so a bad name can only pick a different
        name, never a different directory.
        """
        try:
            return call(_winfs.final_path(self.handle))
        except OSError as exc:
            exc.filename = self.path
            raise

    def listdir(self):
        return self._list(os.listdir)

    def scandir(self):
        return self._list(os.scandir)

    def _open_file(self, name, access, share, *, create=False, open_if=False):
        _check_leaf(name)
        with _located(self, name):
            return _winfs.open_child_file(self.handle, name, access, share, create=create, open_if=open_if,
                                          filename=name)

    def open(self, name, flags, mode=0o666):
        """Open (and with ``O_CREAT`` create) a file here as a binary descriptor, never following a link.

        ``O_CREAT | O_EXCL`` is ``FILE_CREATE`` object-relative to this handle: it
        fails on any existing name, a dangling link included. A final reparse
        point is opened as itself and refused with ``ELOOP``. ``O_TRUNC`` and
        ``O_APPEND`` are not supported: nothing here overwrites a file it did not
        read.
        """
        if flags & _WRITE_FLAGS:
            self._mutation(name)
        rel = self._child_rel(name)
        self._still_plain(rel)
        if flags & (os.O_TRUNC | os.O_APPEND):
            raise OSError(errno.EINVAL, "unsupported open flags", name)
        access = {os.O_RDONLY: _winfs.GENERIC_READ, os.O_WRONLY: _winfs.GENERIC_WRITE,
                  os.O_RDWR: _winfs.GENERIC_READ | _winfs.GENERIC_WRITE}[flags & 3]
        share = _winfs.FILE_SHARE_READ | _winfs.FILE_SHARE_WRITE | _winfs.FILE_SHARE_DELETE
        created = bool(flags & os.O_CREAT) and bool(flags & os.O_EXCL)
        handle = self._open_file(name, access, share, create=created,
                                 open_if=bool(flags & os.O_CREAT) and not flags & os.O_EXCL)
        try:
            info = _winfs.file_info(handle)
            if info.is_reparse_point:
                raise OSError(errno.ELOOP, "path is a reparse point", name)
            if not self._inside(handle, info):
                raise OSError(errno.ELOOP, "entry is not inside the held directory", name)
            self._still_plain(rel)
        except BaseException:
            _winfs.close(handle)
            if created:
                # The create already made this name, so a failed check would strand an
                # empty file the caller never learns about. Only a name created
                # exclusively here is removed: a name that already existed failed the
                # create itself, before this block, and is never touched.
                with suppress(OSError, _Refusal):
                    self._unlink_created(name)
            raise
        return _winfs.to_fd(handle, os.O_RDONLY | os.O_BINARY)  # closes the handle itself if it fails

    def link(self, source, target) -> OSError | _Refusal | None:
        """Publish ``target`` as a second name for ``source``, object-relative to this handle.

        Like ``replace``, returns the failure of the checks that run after the
        link instead of raising it: the new name exists by then, so an error
        would report a published record as a failed create.
        """
        self._mutation(target)
        rel = self._child_rel(target)
        self._still_plain(rel)
        _check_leaf(source)
        _check_leaf(target)
        share = _winfs.FILE_SHARE_READ | _winfs.FILE_SHARE_WRITE | _winfs.FILE_SHARE_DELETE
        source_handle = None
        try:
            source_handle = self._open_file(source, _winfs.FILE_READ_ATTRIBUTES | _winfs.FILE_WRITE_ATTRIBUTES,
                                            share)
            info = _winfs.file_info(source_handle)
            if info.is_reparse_point or info.is_directory:
                raise OSError(errno.EINVAL, "the link source is not a regular file", source)
            with _located(self, source, target):
                _winfs.hard_link(target, source_handle, parent=self.handle, target_name=target,
                                 source_name=source)
        finally:
            if source_handle is not None:
                _winfs.close(source_handle)
        try:
            self._still_plain(rel)
        except (OSError, _Refusal) as exc:
            return exc
        return None

    def _target_info(self, name):
        """Attributes of the entry ``name`` here, or None when absent. Never follows a link."""
        share = _winfs.FILE_SHARE_READ | _winfs.FILE_SHARE_WRITE | _winfs.FILE_SHARE_DELETE
        try:
            handle = self._open_file(name, _winfs.FILE_READ_ATTRIBUTES, share)
        except FileNotFoundError:
            return None
        try:
            return _winfs.file_info(handle)
        finally:
            _winfs.close(handle)

    def replace(self, source, target) -> OSError | _Refusal | None:
        """Rename ``source`` over ``target`` in one atomic step, object-relative to this handle.

        The rename is ``FileRenameInformation`` on the opened source handle with
        this directory as ``RootDirectory``, so neither end is resolved by path.
        An entry already at ``target`` is replaced as itself: a link there is
        removed and replaced, never followed, which is what ``os.replace`` does.
        A directory at ``target`` is refused.

        Returns the ``OSError`` or ``_Refusal`` raised by the checks that run
        after the rename, or None. Those checks cannot undo the rename: the
        entry is published whatever they say, so a caller that turned their
        failure into an error would report a committed write as failed and
        invite a retry that duplicates it. Nothing before the rename is
        returned; that is still an error.
        """
        self._mutation(target)
        rel = self._child_rel(target)
        self._still_plain(rel)
        _check_leaf(source)
        _check_leaf(target)
        share = _winfs.FILE_SHARE_READ | _winfs.FILE_SHARE_WRITE | _winfs.FILE_SHARE_DELETE
        existing = self._target_info(target)
        if existing is not None and existing.is_directory:
            raise OSError(errno.EISDIR, "the target is a directory", target)
        source_handle = self._open_file(source, _winfs.MUTATE_FILE_ACCESS, share)
        try:
            info = _winfs.file_info(source_handle)
            if info.is_reparse_point or info.is_directory:
                raise OSError(errno.EINVAL, "the rename source is not a regular file", source)
            with _located(self, source, target):
                _winfs.move(source_handle, target, parent=self.handle, replace=True,
                            source_name=source, target_name=target)
        finally:
            _winfs.close(source_handle)
        _winfs.flush(self.handle)  # best effort: the record's own bytes were already fsynced
        try:
            self._still_plain(rel)
        except (OSError, _Refusal) as exc:
            return exc
        return None

    def unlink(self, name):
        """Remove the entry ``name`` here by marking the object-relative open for deletion."""
        self._mutation(name)
        rel = self._child_rel(name)
        self._still_plain(rel)
        _check_leaf(name)
        with _located(self, name):
            handle = _winfs.open_child_for_delete(self.handle, name, filename=name)
        try:
            if _winfs.file_info(handle).is_directory:
                raise OSError(errno.EISDIR, "is a directory", name)
            with _located(self, name):
                _winfs.delete_on_close(handle, name)
        finally:
            _winfs.close(handle)
        self._still_plain(rel)

    def set_mtime(self, name, stamp):
        """Set the times of a regular file here on its own object-relative handle, never through a link."""
        self._mutation(name)
        rel = self._child_rel(name)
        self._still_plain(rel)
        share = _winfs.FILE_SHARE_READ | _winfs.FILE_SHARE_WRITE | _winfs.FILE_SHARE_DELETE
        handle = self._open_file(name, _winfs.FILE_WRITE_ATTRIBUTES | _winfs.FILE_READ_ATTRIBUTES, share)
        try:
            info = _winfs.file_info(handle)
            if info.is_reparse_point:
                raise OSError(errno.ELOOP, "path is a reparse point", name)
            if info.is_directory:
                raise OSError(errno.EINVAL, "not a regular file", name)
            if not self._inside(handle, info):
                raise OSError(errno.ELOOP, "entry is not inside the held directory", name)
            with _located(self, name):
                _winfs.set_mtime(handle, stamp)
        finally:
            _winfs.close(handle)
        self._still_plain(rel)

    def fsync(self):
        """There is no directory sync on Windows; durability comes from the flushed record bytes."""

    def close(self):
        """Release this handle once no child of it is still held.

        A child holds its parent, so closing an intermediate directory does not
        release the chain above it while the operation still uses a directory
        below it (SPEC 5.3).
        """
        if self._closed:
            return
        self._closed = True
        if self._children == 0:
            self._release()

    def _release(self):
        """Close the handle, then let a closed parent go once its last held child is released.

        The parent link is dropped here, at the real release, not at the logical
        ``close``: a directory that was closed while a child below it is still
        held keeps holding its own parent, so the whole chain stays held.
        """
        handle, self.handle = self.handle, None
        if handle:
            _winfs.close(handle)
        parent, self._parent = self._parent, None
        if parent is not None:
            parent._children -= 1
            if parent._closed and parent._children == 0:
                parent._release()


def _write_all(fd, data):
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def write_temp_at(directory, data, mode=None):
    """Write ``data`` to a new temporary file inside the held ``directory``; return its name.

    The file is created exclusively and without following a link, then fsynced.
    It is created 0600, like ``tempfile.mkstemp``: history and snapshot copies
    hold a record's complete bytes and must never be more readable than a
    private record. ``mode``, when given, is then applied best
    effort; if that fails the file stays 0600, never broader.
    On failure nothing is left behind. Raises ``OSError`` or ``_Refusal``.
    """
    temporary = f".engagements-{os.urandom(8).hex()}.tmp"
    fd = directory.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC
                        | getattr(os, "O_BINARY", 0), 0o600)
    try:
        try:
            if mode is not None and hasattr(os, "fchmod"):  # no POSIX modes on Windows
                with suppress(OSError):  # permission preservation is best effort
                    os.fchmod(fd, mode)
            _write_all(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
    except BaseException:
        with suppress(OSError, _Refusal):
            directory.unlink(temporary)
        raise
    return temporary


def atomic_write_at(directory, name, data):
    """Atomically replace ``name`` inside the held ``directory`` with ``data``.

    Every step is relative to the directory descriptor, so the write lands in
    the directory that was validated even if its pathname has since been
    swapped. Raises ``OSError`` or ``_Refusal``; callers map them to their
    domain errors. Returns the failure of any check that ran after the rename
    (see ``replace``), because by then ``name`` already holds ``data`` and that
    failure is a warning, never an error.
    """
    temporary = write_temp_at(directory, data)
    try:
        after = directory.replace(temporary, name)
        temporary = None
    finally:
        if temporary is not None:
            with suppress(OSError, _Refusal):
                directory.unlink(temporary)
    return after


def atomic_write(path, data):
    """Atomically replace ``path`` with ``data``, by pathname, for callers outside a held directory.

    Returns the failure of the directory sync that runs after the rename, or
    None: like ``atomic_write_at``, the rename has published ``data`` by then,
    so that failure is the caller's warning and never an error.
    """
    path = Path(path)
    temporary = None
    fd = None
    after = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=".engagements-", dir=path.parent)
        out = os.fdopen(fd, "wb")
        fd = None  # Ownership transferred to the file object.
        with out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
        temporary = None
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                try:
                    os.fsync(directory)
                except OSError as exc:
                    if exc.errno not in (errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP):
                        after = exc
            finally:
                os.close(directory)
    except OSError as exc:
        raise EngagementsError("io_error", "Filesystem operation failed.", {"path": str(path)}) from exc
    finally:
        if fd is not None:
            with suppress(OSError):
                os.close(fd)
        if temporary is not None:
            with suppress(OSError):
                os.unlink(temporary)
    return after


def _open_lock_file(path, directory=None):
    """Open or create a lock file as a fresh descriptor on a regular file.

    The final component is never followed: a symlink planted at the lock path,
    dangling or not, fails with ``ELOOP`` instead of creating or touching its
    target outside the library (on Windows any reparse point is refused the same
    way). ``O_NONBLOCK`` keeps a FIFO from stalling the open, and anything that
    is not a regular file is refused before locking.
    With ``directory`` (a held ``_Dir``) the file is opened relative to it, so a
    lock directory swapped after it was validated cannot redirect the lock.
    """
    flags = (os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
             | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0))
    if directory is None and _HANDLES:
        # Windows has no O_NOFOLLOW: go through a held parent so the final component is not followed.
        with _Dir.open_root(Path(path).parent) as parent:
            fd = parent.open(Path(path).name, flags, 0o666)
    elif directory is None:
        fd = os.open(path, flags, 0o666)
    else:
        fd = directory.open(Path(path).name, flags, 0o666)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "lock path is not a regular file")
        return os.fdopen(fd, "r+b", buffering=0)
    except BaseException:
        os.close(fd)
        raise


class FileLock:
    """``flock`` (``msvcrt.locking`` on byte 0 on Windows) on a descriptor opened for this acquisition only (spec A2).

    ``directory`` is the held lock directory; library code always passes it.
    ``path`` names the lock file and is used for its name and for error details.
    """

    def __init__(self, path, timeout=10.0, *, directory=None):
        self.path = Path(path)
        self.timeout = timeout
        self.directory = directory
        self._file = None

    def __enter__(self):
        try:
            handle = _open_lock_file(self.path, self.directory)
            deadline = time.monotonic() + self.timeout
            while True:
                try:
                    if os.name == "nt":  # pragma: no cover
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self._file = handle
                    return self
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                        raise
                    if time.monotonic() >= deadline:
                        raise LockTimeout(self.path) from None
                    time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        except LockTimeout:
            handle.close()
            raise
        except _Refusal as exc:
            raise EngagementsError("io_error", "Filesystem operation failed.",
                                   {"path": str(self.path), "reason": exc.reason}) from None
        except OSError as exc:
            if "handle" in locals():
                handle.close()
            raise EngagementsError("io_error", "Filesystem operation failed.", {"path": str(self.path)}) from exc

    def __exit__(self, exc_type, exc, tb):
        try:
            try:
                if os.name == "nt":  # pragma: no cover
                    self._file.seek(0)
                    msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            finally:
                handle, self._file = self._file, None
                handle.close()
        except OSError as error:
            raise EngagementsError("io_error", "Filesystem operation failed.", {"path": str(self.path)}) from error
