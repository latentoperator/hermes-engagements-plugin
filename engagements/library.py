"""Library discovery, boundary checks, search and record creation (spec 4.1, 4.4, 4.5).

Nothing here imports Hermes. Time arrives as an injected clock, locking is
``fcntl.flock`` (``msvcrt.locking`` on Windows) on a descriptor opened per
acquisition (spec A2), every write below the root is made relative to a held
directory, and no session, profile or cwd state is kept at module level.

Writes need either descriptor-relative calls (``dir_fd``) with ``O_NOFOLLOW``
(POSIX) or held no-follow directory handles (Windows, ``fsutil._WinDir``). A
platform with neither fails closed: every write and lock returns ``io_error`` /
``unsupported_platform`` before anything is created, and reads still work
through path-based calls.
"""

import contextlib
import errno
import os
import re
import secrets
import stat
import threading
import time as time_module
import unicodedata
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time
from pathlib import Path, PurePosixPath

import yaml

from .errors import EngagementsError, LockTimeout
from .fsutil import (
    _CLOEXEC,
    _NOFOLLOW,
    ROOT_CHANGED,
    UNSUPPORTED_PLATFORM,
    _Dir,
    _NotRealDirectory,
    _Refusal,
    _require_safe_writes,
    _safe_writes,
    _write_all,
    normalize_text,
    rev,
    section_key,
)
from .records import (
    _check_text,
    _single,
    derive_filename,
    derive_id,
    parse,
    render_client,
    render_project,
)
from .schema import WALK_ENTRY_LIMIT

if os.name == "nt":  # pragma: no cover - exercised on Windows
    import msvcrt
else:
    import fcntl

DISCOVERY_WINDOW = 65536
MAX_RECORD_BYTES = 1024 * 1024
LOCK_TIMEOUT = 10.0
# The scan cache's bounds: cached paths per root, roots per process, and the window
# around a read inside which a file's mtime is not trusted. ``SCAN_CACHE_ENV`` set
# to 0, off, false or no, or ``_SCAN_CACHE_ENABLED`` set False, reads every file
# instead: the switch that proves the two paths agree. The cache is also POSIX-only,
# because its identity needs a change time that Windows does not expose: see
# ``_scan_for``.
SCAN_CACHE_PATHS = 20_000
SCAN_CACHE_ROOTS = 4
RACY_MTIME_NS = 2 * 1000 * 1000 * 1000
SCAN_CACHE_ENV = "ENGAGEMENTS_SCAN_CACHE"
_SCAN_CACHE_ENABLED = os.environ.get(SCAN_CACHE_ENV, "").strip().lower() not in ("0", "off", "false", "no")
# The library-relative path that names the root itself, used when a walk stopped
# before it had seen every entry: the part of the library in doubt is the whole of it.
ROOT_RELPATH = "."
_MAX_ATTEMPTS = 10000
_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{1,62}")
_KINDS = ("client", "project")
_STATUSES = {
    "client": ("active", "paused", "former"),
    "project": ("active", "historical"),
}
_ALL_STATUSES = ("active", "paused", "former", "historical")
_INACTIVE = ("paused", "former", "historical")
_LINK_UNSUPPORTED = {errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS, errno.EXDEV, errno.EMLINK}
# Win32: INVALID_FUNCTION and NOT_SUPPORTED (a file system without hard links, such as FAT or exFAT),
# NOT_SAME_DEVICE, and TOO_MANY_LINKS.
_WIN_LINK_UNSUPPORTED = {1, 17, 50, 1142}


# --- small helpers -----------------------------------------------------------


def _listdir(directory):
    """Directory listing for collision checks. A seam for race tests."""
    if isinstance(directory, _Dir):
        return directory.listdir()
    return os.listdir(directory)


def _fold(text):
    return unicodedata.normalize("NFC", text).casefold()


def _display(root, path):
    """A path for error details: relative when inside the root."""
    try:
        return PurePosixPath(Path(path).resolve().relative_to(root)).as_posix()
    except (OSError, RuntimeError, ValueError):
        try:
            return PurePosixPath(Path(path).relative_to(root)).as_posix()
        except ValueError:
            return str(path)


def _io_error(root, exc, path=None):
    if path is None:
        path = getattr(exc, "filename", None) or root
    details = {"path": _display(root, path)}
    code = errno.errorcode.get(exc.errno) if getattr(exc, "errno", None) else None
    if code:
        details["errno"] = code
    return EngagementsError("io_error", "Filesystem operation failed.", details)


def _io_failure(root, path, reason):
    return EngagementsError("io_error", "Filesystem operation failed.",
                            {"path": _display(root, path), "reason": reason})


def _invalid(field_name, reason):
    return EngagementsError("invalid_op", "Invalid argument.", {"field": field_name, "reason": reason})


def _read_upto(fd, limit):
    chunks = []
    remaining = limit
    while remaining > 0:
        chunk = os.read(fd, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _publish_exclusive(directory, name, data, *, fallback_lock=None):
    """Publish ``data`` as ``name`` inside the held ``directory`` without ever replacing a file.

    Writes a temporary file created with ``O_EXCL``, then links it to the final
    name, which fails if the name exists. Where hard links are unavailable the
    final name is created exclusively instead. Returns False when the name is
    already taken. Every step is relative to the directory descriptor.

    Record callers supply ``fallback_lock``: the same record lock the writer uses,
    held across the non-atomic final write and fsync. Readers
    can see a prefix, but updates must re-read complete bytes under that lock.
    Library metadata instead uses the caller's existing library lock.
    """
    temporary = f".engagements-new-{secrets.token_hex(8)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _CLOEXEC | _NOFOLLOW
    fd = directory.open(temporary, flags, 0o666)
    try:
        try:
            _write_all(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            directory.link(temporary, name)
        except FileExistsError:
            return False
        except OSError as exc:
            if exc.errno not in _LINK_UNSUPPORTED and getattr(exc, "winerror", None) not in _WIN_LINK_UNSUPPORTED:
                raise
            with fallback_lock if fallback_lock is not None else contextlib.nullcontext():
                return _create_exclusive(directory, name, data)
        directory.fsync()
        return True
    finally:
        with contextlib.suppress(OSError):
            directory.unlink(temporary)


def _create_exclusive(directory, name, data):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _CLOEXEC | _NOFOLLOW
    try:
        fd = directory.open(name, flags, 0o666)
    except FileExistsError:
        return False
    try:
        try:
            _write_all(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
    except BaseException:
        with contextlib.suppress(OSError):
            directory.unlink(name)
        raise
    directory.fsync()
    return True


@contextlib.contextmanager
def _held_lock(root, directory, name, timeout):
    """Take ``fcntl.flock`` (``msvcrt.locking`` on Windows) on ``directory/name`` through a fresh descriptor (spec A2).

    The lock file is opened relative to the verified metadata directory without
    following a link (``O_NOFOLLOW``; a reparse point on Windows), so a planted
    symlink, dangling or not, is refused instead of creating or touching a file
    outside the library. ``O_NONBLOCK`` keeps a FIFO
    from stalling the open, and anything but a regular file is refused before
    locking.
    """
    relpath = directory._child_rel(name)
    if not _safe_writes() or not directory._can_mutate():
        # Without a no-follow open relative to a held directory, the open would create a planted link's target.
        raise _io_failure(root, root / relpath, UNSUPPORTED_PLATFORM)
    flags = os.O_RDWR | os.O_CREAT | _NOFOLLOW | getattr(os, "O_NONBLOCK", 0) | _CLOEXEC
    try:
        fd = directory.open(name, flags, 0o666)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise _io_failure(root, root / relpath, "symlink") from exc
        raise
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise _io_failure(root, root / relpath, "not_a_regular_file")
        deadline = time_module.monotonic() + timeout
        while True:
            try:
                if os.name == "nt":  # pragma: no cover
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                if time_module.monotonic() >= deadline:
                    raise LockTimeout(relpath) from None
                time_module.sleep(min(0.05, max(0, deadline - time_module.monotonic())))
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                if os.name == "nt":  # pragma: no cover
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _as_clock(today):
    if callable(today):
        return today
    if isinstance(today, datetime):
        return lambda: today
    if isinstance(today, date):
        return lambda: datetime.combine(today, time())
    raise TypeError("today must be a clock callable, a datetime or a date")


# --- results -----------------------------------------------------------------


@dataclass
class Entry:
    """One discovered record. Never carries the body."""

    relpath: str
    kind: str | None
    id: str | None
    name: str | None
    name_unreadable: bool
    client: str | None
    status: str | None
    aliases: tuple
    defects: frozenset
    size: int
    rev: str

    @property
    def valid(self):
        return not self.defects

    def candidate(self):
        return {"id": self.id, "kind": self.kind, "name": self.name, "client": self.client,
                "path": self.relpath}


@dataclass
class Discovery:
    entries: list = field(default_factory=list)
    skipped_symlinks: list = field(default_factory=list)
    oversize: list = field(default_factory=list)
    unreadable: list = field(default_factory=list)
    # Directory entries the walk listed, whether they were records or not.
    listed: int = 0
    # True when the walk stopped at ``WALK_ENTRY_LIMIT`` before it had listed
    # everything, so this inventory cannot prove absence of anything.
    incomplete: bool = False
    # A project's ``locations`` from the same read that produced its entry: a list,
    # or None when the record did not carry them intact. Automatic binding reads
    # them from here instead of opening the file a second time.
    project_locations: dict = field(default_factory=dict)


@dataclass
class FindResult:
    results: list
    total: int
    truncated: bool
    malformed_skipped: int
    warnings: list
    next_offset: int | None = None

    def data(self):
        return {"results": self.results, "total": self.total, "truncated": self.truncated,
                "next_offset": self.next_offset}


@dataclass
class CreateResult:
    id: str
    kind: str
    path: str
    rev: str
    similar_overridden: list
    warnings: list

    def data(self):
        return {"id": self.id, "kind": self.kind, "path": self.path, "rev": self.rev,
                "similar_overridden": self.similar_overridden}


# --- the scan cache ----------------------------------------------------------


@dataclass(frozen=True)
class _CachedScan:
    """What one file said at one identity, and when that read happened.

    ``entry`` is the record before library-wide defects are added, or None for a
    file that is not a record. ``locations`` is a project's ``locations`` from the
    same read, as automatic binding needs them: a tuple, or None when the record
    did not carry them intact. Cached entries are shared between walks and never
    mutated; a walk that adds library-wide defects builds new ones.
    """

    identity: tuple
    entry: Entry | None
    locations: tuple | None
    read_at_ns: int

    def trusted(self):
        """False while the file's mtime lies inside the racy window around this read.

        A coarse filesystem can hold two different contents with the same size and
        the same mtime, so a file whose mtime is within two seconds of the moment
        it was read is read again; once that window has passed, an unchanged
        identity is trusted, exactly as git treats a racily clean index entry.
        """
        return self.identity[3] + RACY_MTIME_NS <= self.read_at_ns


def _file_identity(info):
    """The no-follow stat facts that decide whether a cached read is still current.

    ``ctime_ns`` changes on a chmod, an ownership change or a link count change
    even when size and mtime do not, which is what makes the identity hard to
    preserve across a real edit. It exists on POSIX only; ``_scan_for`` is why
    this function is never asked about a Windows file.
    """
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _cache_platform_supported():
    """Whether this platform's stat facts can decide that a cached read is current.

    False on Windows. There ``os.stat`` exposes no change time (``st_ctime_ns`` is
    a creation time), so a file edited in place to the same length whose exact
    nanosecond mtime is restored by ``os.utime`` keeps every fact the identity
    holds, and a stat-only validator would keep serving the old read. No stat-only
    identity can be made safe there, so the cache is simply not used: every
    candidate is read on every walk, exactly as with the cache switched off.
    """
    return os.name != "nt"


class _ScanCache:
    """The process's store of per-file scan results, keyed by root identity and relative path.

    Nothing is written to disk: Markdown stays the only source of truth (spec D2),
    so there is no index to corrupt or to leak. A one-shot CLI call gets no
    benefit; the gateway, TUI and Desktop sessions do, because their banner hook
    and every tool call walk the same unchanged library.

    One lock covers every read and write: Hermes can run a hook and a tool call on
    different threads. A walk replaces its root's map wholesale, which evicts every
    path it did not see. A root over the path bound, or a process over the root
    bound, drops the oldest map rather than growing without limit.
    """

    def __init__(self, *, paths=SCAN_CACHE_PATHS, roots=SCAN_CACHE_ROOTS):
        self._lock = threading.Lock()
        self._roots = OrderedDict()
        self._paths = paths
        self._roots_limit = roots

    def lookup(self, root_key, relpath, identity):
        """The cached read of ``relpath`` when its identity is current and trusted, else None."""
        with self._lock:
            root = self._roots.get(root_key)
            if root is None:
                return None
            cached = root.get(relpath)
            if cached is None or cached.identity != identity or not cached.trusted():
                return None
            self._roots.move_to_end(root_key)
            return cached

    def replace(self, root_key, scanned):
        """Swap in what one walk saw, or drop this root's map when the walk saw too many paths."""
        with self._lock:
            self._roots.pop(root_key, None)
            if len(scanned) > self._paths:
                return
            self._roots[root_key] = scanned
            while len(self._roots) > self._roots_limit:
                self._roots.popitem(last=False)


_SCAN_CACHE = _ScanCache()


class _Scan:
    """One walk's use of the cache: lookups as it goes, one swap when it finishes."""

    def __init__(self, cache, root_key):
        self._cache = cache
        self._root_key = root_key
        self._seen = {}

    def lookup(self, relpath, identity):
        return self._cache.lookup(self._root_key, relpath, identity)

    def remember(self, relpath, identity, entry, locations):
        self._seen[relpath] = _CachedScan(identity, entry, locations, time_module.time_ns())

    def carry(self, relpath, cached):
        """Keep an entry this walk served from the cache, read time and all.

        The walk's map replaces the old one, so a hit has to be carried across or the
        next walk finds nothing. Its read time is not restamped: that moment is the
        one the racy-window rule is about, and it has not changed.
        """
        self._seen[relpath] = cached

    def finish(self):
        self._cache.replace(self._root_key, self._seen)


def _scan_for(root_key):
    """The cache view for one walk, or None when the cache is off or cannot be safe.

    Off when ``_SCAN_CACHE_ENABLED`` is false, and also on Windows, where no
    stat-only identity can tell a preserved-timestamp edit from the file that was
    read (``_cache_platform_supported``). A walk with no cache view reads every
    candidate, which is the cache-off path: the walk budget, paging and response
    bounds do not depend on it.
    """
    if not _SCAN_CACHE_ENABLED or not _cache_platform_supported():
        return None
    return _Scan(_SCAN_CACHE, root_key)


# --- boundary ----------------------------------------------------------------


def _canonicalize_failure(text, exc):
    """``io_error`` for a path that could not be canonicalized, as supplied.

    Built without resolving anything, because the failure is usually that a
    relative path cannot be resolved at all (a deleted current directory).
    ``open_library`` and ``ensure_inside`` both report it this way.
    """
    details = {"path": text}
    code = errno.errorcode.get(exc.errno) if exc.errno is not None else None
    if code:
        details["errno"] = code
    return EngagementsError("io_error", "Filesystem operation failed.", details)


def ensure_inside(root, path):
    """Resolve ``path`` (relative paths are taken from ``root``) and require it to lie inside ``root``.

    Symlinks are resolved first, so ``..`` escapes, absolute paths elsewhere and
    a symlink inside the root that points outside are all ``outside_root``. The
    path need not exist yet. Returns the resolved path.
    """
    if isinstance(root, Library):
        # An opened library's root was resolved when it was opened; it is checked,
        # not re-resolved, so a root replaced since then cannot move the boundary.
        root.verify_root()
    try:
        resolved_root = os.fspath(root.root) if isinstance(root, Library) else os.path.realpath(os.fspath(root))
        text = os.fspath(path)
        if "\0" in text:  # realpath only refuses this on POSIX
            raise ValueError("embedded null character in path")
        if not os.path.isabs(text):
            text = os.path.join(resolved_root, text)
        resolved = os.path.realpath(text)
    except (ValueError, TypeError):  # TypeError: mixed bytes and text
        raise _invalid("path", "invalid_value") from None
    except OSError as exc:  # e.g. a relative root from a deleted cwd
        raise _canonicalize_failure(os.fspath(path), exc) from exc
    try:
        inside = os.path.commonpath([os.path.normcase(resolved_root), os.path.normcase(resolved)]) == os.path.normcase(resolved_root)
    except ValueError:
        inside = False
    if not inside:
        raise EngagementsError("outside_root", "Path is outside the library root.", {"path": os.fspath(path)})
    return Path(resolved)


# --- opening -----------------------------------------------------------------


def _not_real_failure(root, error):
    return _io_failure(root, root / error.relpath, error.reason)


def _read_library_yaml(root, meta):
    """Return the library id, None when library.yaml is absent, or raise library_missing.

    ``meta`` is the held ``.engagements`` directory; the file is opened relative
    to it and never through a symlink.
    """
    path = root / ".engagements" / "library.yaml"
    try:
        fd = meta.open("library.yaml", os.O_RDONLY | _NOFOLLOW | _CLOEXEC | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise _io_failure(root, path, "symlink") from exc
        raise
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise _io_failure(root, path, "not_a_regular_file")
        data = _read_upto(fd, 65537)
    finally:
        os.close(fd)

    def bad():
        return EngagementsError("library_missing", "The library has no valid .engagements/library.yaml.",
                                {"reason": "invalid_library_yaml", "path": ".engagements/library.yaml"})

    if len(data) > 65536:
        raise bad()
    try:
        loaded = yaml.safe_load(data)
    except MemoryError:
        # Not a statement about the file: an out-of-memory load must not be turned
        # into "invalid metadata" and hidden.
        raise
    except Exception:
        # A YAML constructor can fail in many ways, not only with YAMLError: an
        # explicit tag raises whatever the tag's code raises (``!!timestamp nope``
        # raises AttributeError, ``!!int ""`` IndexError). Every one of them means
        # the metadata is unreadable, so the class is caught rather than a list of
        # the types seen so far. KeyboardInterrupt is not an Exception and still
        # propagates.
        raise bad() from None
    if not isinstance(loaded, dict):
        raise bad()
    fmt, identifier = loaded.get("format"), loaded.get("library_id")
    if isinstance(fmt, bool) or fmt != 1 or not isinstance(fmt, int) or not isinstance(identifier, str):
        raise bad()
    try:
        uuid.UUID(identifier)
    except ValueError:
        raise bad() from None
    return identifier


def _resolved_root(root):
    """The root, ``~``-expanded and resolved once; a failure names the root as supplied.

    Resolving a relative root reads the current directory, which can fail (the
    directory may have been deleted); that is ``io_error``, not a raw ``OSError``.
    """
    try:
        text = os.path.expanduser(os.fspath(root))
    except (ValueError, TypeError):
        raise _invalid("library_root", "invalid_value") from None
    try:
        return Path(os.path.realpath(text))
    except (ValueError, TypeError):  # TypeError: a bytes root
        raise _invalid("library_root", "invalid_value") from None
    except OSError as exc:
        raise _canonicalize_failure(text, exc) from exc


def open_library(root, *, create_default):
    """Open (or, for the default root, create) a library. The root is resolved once.

    This is the opener for every path that may write. A caller that must not
    write uses ``open_library_readonly`` instead.
    """
    resolved = _resolved_root(root)
    missing = EngagementsError("library_missing", "The library root does not exist or is not a library.",
                               {"reason": "root_missing"})
    try:
        if not resolved.exists():
            if not create_default:
                raise missing
            _require_safe_writes()
            resolved.mkdir(parents=True, exist_ok=True)
        elif not resolved.is_dir():
            raise EngagementsError("library_missing", "The library root is not a directory.",
                                   {"reason": "root_not_a_directory"})
        with _Dir.open_root(resolved) as top:
            return _open_held(resolved, top, create_default)
    except _Refusal as exc:
        raise _not_real_failure(resolved, exc) from None
    except OSError as exc:
        raise _io_error(resolved, exc) from exc


def _open_held(resolved, top, create_default):
    meta = top.open_dir(".engagements")
    try:
        invalid = None
        identifier = None
        if meta is not None:
            try:
                identifier = _read_library_yaml(resolved, meta)
            except EngagementsError as exc:
                if exc.code != "library_missing":
                    raise
                invalid = exc
        if identifier is not None:
            return Library(resolved, identifier, False, top.identity())
        if invalid is not None:
            # An empty or partial library.yaml can be one that a concurrent first use
            # is still writing (the exclusive-create fallback is not atomic). Every
            # initialization runs under the library lock, so re-read under it. A file
            # that is still invalid then is genuinely invalid and is never overwritten.
            locks = meta.open_dir("locks")
            if locks is None:
                raise invalid
            with locks, _held_lock(resolved, locks, "_library.lock", LOCK_TIMEOUT):
                identifier = _read_library_yaml(resolved, meta)
            if identifier is None:
                raise invalid
            return Library(resolved, identifier, False, top.identity())
        if not create_default:
            raise EngagementsError("library_missing", "The library has no .engagements/library.yaml.",
                                   {"reason": "missing_library_yaml", "path": ".engagements/library.yaml"})
        _require_safe_writes(".engagements")
        if meta is None:
            meta = top.ensure_dir(".engagements")
        for sub in ("history", "snapshots"):
            meta.ensure_dir(sub).close()
        with meta.ensure_dir("locks") as locks, _held_lock(resolved, locks, "_library.lock", LOCK_TIMEOUT):
            identifier = _read_library_yaml(resolved, meta)
            if identifier is not None:
                return Library(resolved, identifier, False, top.identity())
            candidate = str(uuid.uuid4())
            if not _publish_exclusive(meta, "library.yaml", f"format: 1\nlibrary_id: {candidate}\n".encode()):
                # Only a writer that does not take the lock can get here.
                identifier = _read_library_yaml(resolved, meta)
                if identifier is None:
                    raise _io_failure(resolved, resolved / ".engagements/library.yaml", "unreadable")
                return Library(resolved, identifier, False, top.identity())
            return Library(resolved, candidate, True, top.identity())
    finally:
        if meta is not None:
            meta.close()


def open_library_readonly(root):
    """Open an existing library without creating, locking or writing anything (SPEC 6.7).

    Resolves and classifies the same way as ``open_library(root, create_default=False)``,
    with one deliberate difference: an invalid ``library.yaml`` is reported from the one
    read of the held metadata directory. That opener re-reads an invalid file under the
    library lock, which can create ``.engagements/locks/_library.lock``; a read-only view
    must not write. ``doctor._metadata_state`` classifies the same way
    for the same reason. Nothing is created here: no lock, no directory, no metadata.
    """
    resolved = _resolved_root(root)
    if not resolved.exists():
        raise EngagementsError("library_missing", "The library root does not exist or is not a library.",
                               {"reason": "root_missing"})
    if not resolved.is_dir():
        raise EngagementsError("library_missing", "The library root is not a directory.",
                               {"reason": "root_not_a_directory"})
    try:
        with _Dir.open_root(resolved, writable=False) as top:
            meta = top.open_dir(".engagements")
            if meta is None:
                raise EngagementsError("library_missing", "The library has no .engagements/library.yaml.",
                                       {"reason": "missing_library_yaml", "path": ".engagements/library.yaml"})
            try:
                identifier = _read_library_yaml(resolved, meta)
            finally:
                meta.close()
            if identifier is None:
                raise EngagementsError("library_missing", "The library has no .engagements/library.yaml.",
                                       {"reason": "missing_library_yaml", "path": ".engagements/library.yaml"})
            return Library(resolved, identifier, False, top.identity())
    except _Refusal as exc:
        raise _not_real_failure(resolved, exc) from None
    except OSError as exc:
        raise _io_error(resolved, exc) from exc


# --- discovery ----------------------------------------------------------------


@dataclass(frozen=True)
class _Scanned:
    """What one read of one candidate said, when the read may be cached.

    ``entry`` is None for a readable file that is not a record. ``locations`` is a
    project's ``locations`` from the same parse: a tuple, or None when the record
    did not carry them intact.
    """

    entry: Entry | None = None
    locations: tuple | None = None


def _scan_candidate(directory, name, relpath, discovery, listed, scan):
    """Read one listed ``.md`` candidate, or take the cache's word for an unchanged file.

    The stat is the walk's own, no-follow one: it is what the cache validates
    against and costs nothing extra for a file that has to be read anyway. A file
    that cannot be stated falls through to ``_scan_file``, which classifies it as
    before. Files that are not regular, unreadable, oversize or that raised are
    never remembered, so the next walk examines them again.
    """
    try:
        info = listed.stat(follow_symlinks=False)
    except OSError:
        _scan_file(directory, name, relpath, discovery, listed)
        return
    if not stat.S_ISREG(info.st_mode):
        return
    if scan is None:
        _scan_file(directory, name, relpath, discovery, listed)
        return
    identity = _file_identity(info)
    cached = scan.lookup(relpath, identity)
    if cached is not None:
        scan.carry(relpath, cached)
        if cached.entry is not None:
            discovery.entries.append(cached.entry)
            if cached.entry.kind == "project":
                discovery.project_locations[relpath] = cached.locations
        return
    scanned = _scan_file(directory, name, relpath, discovery, listed)
    if scanned is not None:
        scan.remember(relpath, identity, scanned.entry, scanned.locations)


def _scan_file(directory, name, relpath, discovery, entry=None):
    """Read one candidate. At most 64 KiB is read unless the file proves to be a record.

    Opened relative to its held ``directory`` without following a final link,
    so a directory swapped since it was opened cannot redirect the read.

    When the open fails, the listed ``entry``'s no-follow stat still decides a
    regular file over 1 MiB: it can never be a record, so it is ``oversize``,
    not ``unreadable``, and does not make create refuse.
    Anything else that cannot be opened stays ``unreadable``.

    Returns what the read said when the file may be cached, and None when it may
    not: an unreadable, oversize or non-regular file, or anything that raised.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = directory.open(name, flags)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            discovery.skipped_symlinks.append(relpath)
            return None
        size = _regular_size(entry)
        if size is not None and size > MAX_RECORD_BYTES:
            discovery.oversize.append((relpath, size))
        else:
            discovery.unreadable.append(relpath)
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return None
        if info.st_size > MAX_RECORD_BYTES:
            discovery.oversize.append((relpath, info.st_size))
            return None
        head = _read_upto(fd, DISCOVERY_WINDOW)
        if not (head.startswith(b"---") or head.startswith(b"\xef\xbb\xbf---")):
            return _Scanned()
        # The window's end is end of file only when the file really ends there. When
        # it continues, a '---' at the very end of the window may be the start of a
        # longer line, so only complete lines inside the window may close the
        # frontmatter: a closing line whose EOL falls outside the window is past the
        # 65,536-byte limit (spec C6) and the file is not read further.
        at_eof = len(head) < DISCOVERY_WINDOW or info.st_size <= len(head)
        window = head if at_eof else head[:head.rfind(b"\n") + 1]
        record = parse(window)
        if not record.is_record:
            return _Scanned()
        data = head
        if not at_eof:
            data = head + _read_upto(fd, MAX_RECORD_BYTES + 1 - len(head))
            if len(data) > MAX_RECORD_BYTES:
                discovery.oversize.append((relpath, len(data)))
                return None
            record = parse(data)
            if not record.is_record:
                return _Scanned()
    except OSError:
        discovery.unreadable.append(relpath)
        return None
    finally:
        os.close(fd)
    entry = _entry(relpath, data, record)
    discovery.entries.append(entry)
    locations = _record_locations(record) if entry.kind == "project" else None
    if entry.kind == "project":
        discovery.project_locations[relpath] = locations
    return _Scanned(entry, locations)


def _record_locations(record):
    """A project's ``locations`` as its own parse read them: a tuple, or None when untrustworthy.

    Unknown is not the same as empty. A project that lists no folder contributes
    no locations and one whose folders could not be trusted must not be mistaken
    for it, so None is returned for every way the property can fail to arrive
    intact: no properties at all, a defect recorded against the property, a value
    that is not a list, or a list holding a non-string. This is the same verdict
    automatic binding used to take from a second read of the file, taken instead
    from the one read that produced the entry, so the two can never disagree.
    """
    properties = record.properties
    if properties is None or "locations" in record.property_defects:
        return None
    if "locations" not in properties:
        return ()
    value = properties["locations"]
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        return None
    return tuple(value)


def _regular_size(entry):
    """The size of a listed regular file from a no-follow stat, or None."""
    if entry is None:
        return None
    try:
        info = entry.stat(follow_symlinks=False)
    except OSError:
        return None
    return info.st_size if stat.S_ISREG(info.st_mode) else None


def _entry(relpath, data, record):
    properties = record.properties or {}
    defects = set(record.defects) | set(record.property_defects.values()) | set(record.section_defects.values())
    # TOOLS.md: `client` is null for clients. An extra `client` property on a client
    # stays in the record; it is not a relationship.
    client = properties.get("client") if record.kind == "project" else None
    aliases = properties.get("aliases")
    status = properties.get("status", "active") if record.properties is not None else None
    if "status" in record.property_defects:
        status = None  # a defective status (duplicate key, unsupported YAML) is unreadable
    return Entry(
        relpath=relpath,
        kind=record.kind,
        id=record.id if isinstance(record.id, str) else None,
        name=record.name if isinstance(record.name, str) else None,
        name_unreadable=record.name_unreadable,
        client=client if isinstance(client, str) else None,
        status=status if isinstance(status, str) else None,
        aliases=tuple(aliases) if isinstance(aliases, list) and all(isinstance(a, str) for a in aliases) else (),
        defects=frozenset(defects),
        size=len(data),
        rev=rev(data),
    )


def _children(directory, relative, discovery, *, root):
    """The name-sorted children of a held ``directory``; empty when it cannot be listed.

    A root that cannot be listed is the library's own failure and propagates; a
    subdirectory that cannot be listed is recorded as unreadable and skipped, so
    one unreadable subtree never hides the rest of the library.

    Every child counts against the traversal budget, whatever it is: a library
    that points at a large notes vault pays for the vault's notes as well as its
    records, and that is what the budget bounds. The budget is enforced while the
    directory is being read, not after it: only the entries this walk may still
    list are buffered, and one more is read to learn that the directory does not
    fit. A directory that does not fit is discarded whole and the walk is marked
    incomplete, so which of its children a bounded walk keeps never depends on
    the order the filesystem happened to return them in.
    """
    remaining = max(0, WALK_ENTRY_LIMIT - discovery.listed)
    try:
        with directory.scandir() as scanner:
            children = []
            for child in scanner:
                if len(children) >= remaining:
                    # One entry past the allowance: this listing cannot be
                    # finished inside it, so none of it is used.
                    discovery.listed += len(children) + 1
                    discovery.incomplete = True
                    return []
                children.append(child)
            children.sort(key=lambda entry: entry.name)
    except OSError:
        if root:
            raise
        discovery.unreadable.append(relative)
        return []
    discovery.listed += len(children)
    return children


def _walk(directory, relative, discovery, scan=None):
    """Discover records below the held ``directory`` (a ``_Dir``), with an explicit stack.

    Every subdirectory is opened without following a link relative to its held
    parent and held while it is scanned, and every candidate is opened relative
    to its held directory (TOOLS.md "Filesystem boundary"). A
    pathname is never re-resolved, so an entry swapped for a symlink after it
    was listed is skipped rather than followed out of the library.

    The walk keeps its own stack of held directories instead of recursing
    for that reason: recursion spent two Python frames per level, so a tree a
    few hundred levels deep -- well inside the filesystem path limit -- raised
    ``RecursionError`` and hid every record, shallow ones included. Order is
    unchanged: depth first, children by name, files and directories interleaved.
    The cost is one held descriptor per level, which the recursive version also
    paid; a level that cannot be opened is recorded as unreadable, never raised.
    The ``finally`` releases every held frame, so an ``OSError`` or a
    ``BaseException`` such as ``KeyboardInterrupt`` still closes all of them.

    ``scan`` carries the process's scan cache when it is in use; without it every
    candidate is read. Once the walk has listed more than ``WALK_ENTRY_LIMIT``
    entries it stops where it is and marks the discovery incomplete: the caller
    then has what was read, and no claim about what was not. A directory that
    cannot be listed inside what is left of the budget is dropped whole and has
    the same effect, so the walk reads no further than the budget allows even
    when one directory alone holds more entries than the whole library may list.
    """
    stack = []  # [directory, relative, children, index, owned]
    try:
        stack.append([directory, relative, _children(directory, relative, discovery, root=not relative), 0, False])
        while stack:
            if discovery.listed > WALK_ENTRY_LIMIT:
                discovery.incomplete = True
                break
            frame = stack[-1]
            directory, relative, children, index, _owned = frame
            if index >= len(children):
                stack.pop()
                if frame[4]:
                    directory.close()
                continue
            child = children[index]
            frame[3] = index + 1
            name = child.name
            rel = f"{relative}/{name}" if relative else name
            try:
                if child.is_symlink():
                    if not name.startswith(".") and (name.endswith(".md") or _link_may_be_dir(child)):
                        discovery.skipped_symlinks.append(rel)
                elif child.is_dir(follow_symlinks=False):
                    if not name.startswith("."):
                        _descend(directory, name, rel, discovery, stack)
                elif child.is_file(follow_symlinks=False) and name.endswith(".md"):
                    _scan_candidate(directory, name, rel, discovery, child, scan)
            except OSError:
                discovery.unreadable.append(rel)
    finally:
        while stack:
            frame = stack.pop()
            if frame[4]:
                frame[0].close()


def _link_may_be_dir(entry):
    """Whether a symlink is worth reporting as a skipped directory link.

    The link is excluded whatever it points at; this only decides whether it is
    reported. A target that cannot be checked (a link loop, a target below an
    inaccessible directory) is reported rather than counted as unreadable: an
    unreadable entry would make create refuse, and a symlink is never a record.
    """
    try:
        return entry.is_dir()
    except OSError:
        return True


def _descend(directory, name, relative, discovery, stack):
    """Hold the child directory and push it on the walk stack, or record why it was not entered.

    The walk only reads, so each directory is held read-only: no other process is
    denied write access to it for the length of a scan, and the kernel refuses any
    create through the handle.
    """
    try:
        held = directory.open_dir(name, writable=False)
    except _NotRealDirectory:
        discovery.skipped_symlinks.append(relative)  # replaced by a link or a file since it was listed
        return
    if held is None:
        return  # removed since it was listed
    try:
        children = _children(held, relative, discovery, root=False)
    except BaseException:
        held.close()
        raise
    if not children:
        held.close()  # nothing to read below it
        return
    stack.append([held, relative, children, 0, True])


def _require_complete_inventory(root, discovery):
    """Refuse a mutation whose inventory is incomplete.

    An unreadable record or subtree holds ids, names and aliases the scan never
    saw, so this inventory cannot prove an id is free or that nothing similar
    exists (SPEC 4.4's single id namespace, TOOLS.md create C1). Candidates the
    walk excludes on purpose -- symlinks, oversize files, dot-directories -- are
    not read failures and never block a write.

    A walk that stopped at the traversal budget is incomplete for the same reason
    and is refused the same way: everything past the limit is unread, not absent.
    """
    if discovery.incomplete:
        raise _io_failure(root, Path(root), "incomplete_scan")
    if discovery.unreadable:
        # Anchored at the root: a bare relative path would resolve against the cwd.
        raise _io_failure(root, Path(root) / discovery.unreadable[0], "incomplete_scan")


def _library_wide_defects(entries):
    """Add duplicate_id and unknown_client, which only a view of the whole library can see."""
    counts = {}
    clients = set()
    for entry in entries:
        if entry.id is not None:
            counts[entry.id] = counts.get(entry.id, 0) + 1
            if entry.kind == "client":
                clients.add(entry.id)
    result = []
    for entry in entries:
        extra = set()
        if entry.id is not None and counts[entry.id] > 1:
            extra.add("duplicate_id")
        if entry.kind == "project" and entry.client is not None and entry.client not in clients:
            extra.add("unknown_client")
        result.append(replace(entry, defects=entry.defects | extra) if extra else entry)
    return result


# --- argument validation --------------------------------------------------------


def _validate_find(query, kind, include_inactive, limit, offset):
    if not isinstance(query, str) or len(query) > 200:
        raise _invalid("query", "invalid_value")
    if kind is not None and kind not in _KINDS:
        raise _invalid("kind", "invalid_value")
    if not isinstance(include_inactive, bool):
        raise _invalid("include_inactive", "invalid_value")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise _invalid("limit", "invalid_value")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise _invalid("offset", "invalid_value")


def _validate_create(kind, name, client, properties, sections, source, allow_similar):
    """Stage 1 for engagements_create. Returns normalized properties and sections."""
    # Reserved names are recognized before generic schema rejection (TOOLS.md precedence).
    if isinstance(properties, dict) and "updated" in properties:
        raise _invalid("properties.updated", "plugin_owned")
    if isinstance(sections, list):
        for index, item in enumerate(sections):
            if (isinstance(item, dict) and isinstance(item.get("section"), str)
                    and section_key(item["section"]) == "change log"):
                raise _invalid(f"sections[{index}].section", "plugin_owned")
    if kind not in _KINDS:
        raise _invalid("kind", "invalid_value")
    if not _single(name, 200):
        raise _invalid("name", "invalid_value")
    if kind == "project":
        if client is None:
            raise _invalid("client", "missing_field")
        if not isinstance(client, str) or not _ID_PATTERN.fullmatch(client):
            raise _invalid("client", "invalid_value")
    elif client is not None:
        raise _invalid("client", "key_not_allowed_for_kind")
    clean = _validate_properties(kind, properties)
    items = _validate_sections(sections)
    if not _single(source, 200):
        raise _invalid("source", "invalid_value")
    if not isinstance(allow_similar, bool):
        raise _invalid("allow_similar", "invalid_value")
    return clean, items


def _validate_properties(kind, properties):
    if properties is None:
        return {}
    if not isinstance(properties, dict):
        raise _invalid("properties", "invalid_value")
    for key in properties:
        if key not in ("aliases", "status", "locations"):
            raise _invalid(f"properties.{key}", "unexpected_field")
    clean = {}
    if "aliases" in properties:
        aliases = properties["aliases"]
        if not isinstance(aliases, list) or len(aliases) > 20 or not all(_single(a, 200) for a in aliases):
            raise _invalid("properties.aliases", "invalid_value")
        seen, unique = set(), []
        for alias in aliases:
            normalized = normalize_text(alias)
            if normalized not in seen:
                seen.add(normalized)
                unique.append(alias)
        clean["aliases"] = unique
    if "status" in properties:
        status = properties["status"]
        if not isinstance(status, str) or status not in _ALL_STATUSES:
            raise _invalid("properties.status", "invalid_value")
        if status not in _STATUSES[kind]:
            raise _invalid("properties.status", "status_not_allowed_for_kind")
        clean["status"] = status
    if "locations" in properties:
        if kind != "project":
            raise _invalid("properties.locations", "key_not_allowed_for_kind")
        locations = properties["locations"]
        if not isinstance(locations, list) or len(locations) > 50 or not all(_single(x, 1024) for x in locations):
            raise _invalid("properties.locations", "invalid_value")
        clean["locations"] = list(locations)
    return clean


def _validate_sections(sections):
    if sections is None:
        return []
    if not isinstance(sections, list) or len(sections) > 30:
        raise _invalid("sections", "invalid_value")
    seen = set()
    items = []
    for index, item in enumerate(sections):
        prefix = f"sections[{index}]"
        if not isinstance(item, dict):
            raise _invalid(prefix, "invalid_value")
        for required in ("section", "body"):
            if required not in item:
                raise _invalid(f"{prefix}.{required}", "missing_field")
        if not _single(item["section"], 200):
            raise _invalid(f"{prefix}.section", "invalid_value")
        key = section_key(item["section"])
        if key in seen:
            raise _invalid(f"{prefix}.section", "duplicate_section")
        seen.add(key)
        body = item["body"]
        if (not isinstance(body, str) or len(body) > 20000
                or any(0xD800 <= ord(c) <= 0xDFFF for c in body)):
            raise _invalid(f"{prefix}.body", "invalid_value")
        try:
            _check_text(body, None, "body")
        except EngagementsError as exc:
            raise _invalid(f"{prefix}.body", exc.details["reason"]) from None
        for extra in item:
            if extra not in ("section", "body"):
                raise _invalid(f"{prefix}.{extra}", "unexpected_field")
        items.append({"section": item["section"], "body": body})
    return items


def _render(kind, id_, name, client, clock, profile, source, properties, sections):
    render = render_client if kind == "client" else render_project
    arguments = {"clock": clock, "profile": profile, "source": source}
    positional = (id_, name) if kind == "client" else (id_, name, client)
    try:
        data = render(*positional, properties=properties, sections=sections, **arguments)
    except EngagementsError as exc:
        if exc.code == "invalid_op" and exc.details.get("reason") == "structure_changed":
            # The renderer re-parses its output. An oversized frontmatter looks the same as a
            # section that does not round-trip; telling them apart takes a render without sections.
            try:
                render(*positional, properties=properties, sections=None, **arguments)
            except EngagementsError as probe:
                if probe.details.get("reason") == "structure_changed":
                    raise _invalid("properties", "too_large") from None
                raise
        raise
    if len(data) > MAX_RECORD_BYTES:
        raise _invalid("sections", "too_large")
    parsed = parse(data)
    if parsed.frontmatter_span is None or parsed.frontmatter_span[1] > DISCOVERY_WINDOW:
        raise _invalid("properties", "too_large")
    return data


# --- the library --------------------------------------------------------------


@dataclass(eq=False)
class Library:
    """An opened library. ``root`` was resolved once, when it was opened (spec 4.1).

    ``root_id`` is the opened root directory's identity. Every later operation
    reopens the root without following a link and checks that identity, so a
    root, or one of its ancestors, replaced after opening is refused with
    ``io_error`` / ``root_changed`` instead of being followed.
    """

    root: Path
    library_id: str
    created: bool = False
    root_id: tuple | None = field(default=None, repr=False)

    # --- plumbing

    def base_warnings(self):
        if not self.created:
            return []
        return [{"code": "library_created", "message": "Created a new library at the default location."}]

    def open_root(self):
        """The opened root, held by descriptor. Raises ``_Refusal`` (``root_changed``) or ``OSError``.

        Refused when the root's pathname is now a symlink, is no longer canonical,
        or leads to a different directory than the one that was opened.
        """
        top = _Dir.open_root(self.root, self.root_id)
        try:
            if os.path.realpath(self.root) != os.fspath(self.root):
                raise _NotRealDirectory("", ROOT_CHANGED)
        except BaseException:
            top.close()
            raise
        return top

    def verify_root(self):
        """Raise ``io_error`` / ``root_changed`` unless the root is still the directory that was opened."""
        try:
            self.open_root().close()
        except _Refusal as exc:
            raise _not_real_failure(self.root, exc) from None
        except OSError as exc:
            raise _io_error(self.root, exc) from exc

    def _meta_dir(self, name):
        """A held, real metadata subdirectory, created when absent. Never reached through a symlink."""
        with self.open_root() as top, top.ensure_dir(".engagements") as meta:
            return meta.ensure_dir(name)

    def discover(self, fresh=False):
        """Discover every record under the root.

        ``fresh`` reads every file it finds. It is the switch for the two scans in
        ``create`` and for ``doctor``, and nothing else uses it; every other caller
        reuses the process's scan cache, which opens only the files whose identity
        changed since they were last read, and on a platform with no change time
        every caller reads every file (``_scan_for``). The walk itself always
        happens: duplicate ids and unknown clients depend on the whole set, so no
        caller may skip it.

        The opened root is held for the whole walk: a root replaced after this
        check cannot redirect discovery.
        """
        discovery = Discovery()
        scan = None
        try:
            with self.open_root() as top:
                if not fresh:
                    scan = _scan_for(top.identity())
                _walk(top, "", discovery, scan)
        except _Refusal as exc:
            raise _not_real_failure(self.root, exc) from None
        except OSError as exc:
            raise _io_error(self.root, exc, self.root) from exc
        if scan is not None:
            scan.finish()
        discovery.entries = _library_wide_defects(discovery.entries)
        return discovery

    def library_defects(self, record, relpath, *, discovery):
        """Library-wide defects of one record, for the writer's stage-4 check.

        ``record`` is the parse of the locked bytes; ``relpath`` is where it lives.
        The record's own entry is excluded, so what is reported is how the rest of
        the library sees it.
        """
        others = [e for e in discovery.entries if e.relpath != relpath]
        defects = set()
        if isinstance(record.id, str) and any(e.id == record.id for e in others):
            defects.add("duplicate_id")
        client = (record.properties or {}).get("client")
        if (record.kind == "project" and isinstance(client, str)
                and not any(e.kind == "client" and e.id == client for e in others)):
            defects.add("unknown_client")
        return defects

    def record_path(self, path):
        """Validate a caller-supplied record path and return it relative to the root.

        ``outside_root`` for escapes; ``not_found`` / ``excluded`` for dot-directory
        or symlink components; ``not_found`` / ``not_a_record`` for non-.md names.
        """
        resolved = ensure_inside(self, path)
        text = os.fspath(path)
        lexical = os.path.abspath(text if os.path.isabs(text) else os.path.join(self.root, text))
        try:
            relative = resolved.relative_to(self.root)
        except ValueError:  # the root moved between the check and here
            raise EngagementsError("outside_root", "Path is outside the library root.", {"path": text}) from None
        excluded = EngagementsError("not_found", "Path is excluded from the library.", {"reason": "excluded"})
        if os.path.normcase(lexical) != os.path.normcase(str(resolved)):
            raise excluded
        if any(part.startswith(".") for part in relative.parts[:-1]):
            raise excluded
        if not relative.parts or not relative.parts[-1].endswith(".md"):
            raise EngagementsError("not_found", "Path is not a record.", {"reason": "not_a_record"})
        return relative.as_posix()

    # --- find and resolve

    def find(self, query, kind=None, include_inactive=False, limit=20, offset=0, *, discovery=None):
        """One page of matches, in a stable order (TOOLS.md, ``engagements_find``).

        The order is the sort key below: tier high first, then clients before
        projects, then normalized name, id and path. ``offset`` selects a window
        of that order, so consecutive pages never overlap or skip while the
        library's records say the same thing.
        """
        _validate_find(query, kind, include_inactive, limit, offset)
        discovery = discovery or self.discover()
        needle = normalize_text(query)
        rows = []
        skipped = 0
        for entry in discovery.entries:
            # A readable kind and id make a record listable even when its name is
            # unreadable, so a mistyped identity field is shown as invalid rather than
            # dropped. The exception is the record's own name being present with a
            # value that is not a string (including a mapping or a list); any other
            # defect, on any other field, leaves a nameless record counted only, and
            # never reorders the records that were listable before.
            if entry.kind is None or entry.id is None or (
                    entry.name is None and not entry.name_unreadable):
                if entry.kind is None or kind is None or entry.kind == kind:
                    skipped += 1
                continue
            if kind is not None and entry.kind != kind:
                continue
            # Only the statuses TOOLS.md names as inactive hide a record. Any other
            # status, including an unreadable one, stays visible.
            if not include_inactive and entry.status in _INACTIVE:
                continue
            tier, match = _match(entry, needle)
            if tier == 0:
                continue
            rows.append(((-tier, 0 if entry.kind == "client" else 1, normalize_text(entry.name or ""),
                          entry.id, entry.relpath), entry, match))
        rows.sort(key=lambda row: row[0])
        page = rows[offset:offset + limit]
        results = [{"id": e.id, "kind": e.kind, "name": e.name, "status": e.status, "client": e.client,
                    "path": e.relpath, "valid": e.valid, "match": m} for _, e, m in page]
        truncated = offset + len(page) < len(rows)
        warnings = self.base_warnings()
        if discovery.incomplete:
            warnings.append({"code": "discovery_incomplete",
                             "message": "The library is too large to scan in full, so records past the "
                                        f"{WALK_ENTRY_LIMIT:,} directory entries the scan read are not listed. "
                                        "Run doctor, and point library_root at a narrower folder."})
        if skipped:
            warnings.append({"code": "malformed_skipped",
                             "message": f"{skipped} malformed record(s) without a readable kind, id and name "
                                        "were skipped. Run doctor."})
        return FindResult(results, len(rows), truncated, skipped, warnings,
                          offset + len(page) if truncated else None)

    def resolve(self, ref, kind=None, *, client=None, discovery=None):
        """Resolve an id, or one exact normalized name or alias, to a single Entry (TOOLS.md T3)."""
        if not _single(ref, 200):
            raise _invalid("ref", "invalid_value")
        if kind is not None and kind not in _KINDS:
            raise _invalid("kind", "invalid_value")
        discovery = discovery or self.discover()
        entries = discovery.entries
        # Ids are global (T3): multiplicity is counted over every record, of any kind
        # and including null-kind ones, before any kind or client filter applies.
        counts = {}
        for entry in entries:
            if entry.id is not None:
                counts[entry.id] = counts.get(entry.id, 0) + 1
        same_id = [e for e in entries if e.id == ref]
        if len(same_id) > 1:
            raise _ambiguous(same_id)
        if same_id and same_id[0].kind is not None and (kind is None or same_id[0].kind == kind):
            return same_id[0]
        pool = [e for e in entries
                if e.kind is not None and e.id is not None and (kind is None or e.kind == kind)]
        wanted = normalize_text(ref)
        scoped = [e for e in pool if client is None or (e.kind == "project" and e.client == client)]
        matches = [e for e in scoped if wanted and (
            (e.name is not None and normalize_text(e.name) == wanted)
            or any(normalize_text(a) == wanted for a in e.aliases))]
        if len(matches) == 1 and counts[matches[0].id] == 1:
            return matches[0]
        if matches:
            # A duplicated id never selects a record, even through its name or alias:
            # every record sharing that id is a candidate, so each can be read by path.
            duplicated = {m.id for m in matches if counts[m.id] > 1}
            candidates = {e.relpath: e for e in matches}
            for entry in entries:
                if entry.id in duplicated:
                    candidates.setdefault(entry.relpath, entry)
            raise _ambiguous(candidates.values())
        raise EngagementsError("not_found", f"No client or project matches '{ref}'.", {"reason": "no_match"})

    # --- create

    def create(self, kind, name, client=None, properties=None, sections=None, source="conversation",
               allow_similar=False, *, profile, today):
        """Create a record from the template without ever overwriting a file (TOOLS.md create).

        ``today`` is the injected clock: a callable returning an aware datetime. A fixed
        datetime or date is accepted for convenience and wrapped in a constant clock.
        """
        clean, items = _validate_create(kind, name, client, properties, sections, source, allow_similar)
        clock = _as_clock(today)
        if profile is None or profile == "":
            profile = "default"
        if not _single(profile, 200):
            raise _invalid("profile", "invalid_value")
        try:
            _require_safe_writes()  # nothing is created where a write could be redirected
            if kind == "project":
                # The pre-lock check is only a fast path. An incomplete inventory
                # cannot answer it, and guessing "no such client" there would be a
                # misleading error, so the under-lock check decides.
                initial = self.discover(fresh=True)
                if not initial.unreadable and not initial.incomplete:
                    _require_client(client, initial)  # stage 4, before the lock
            with self._meta_dir("locks") as locks, _held_lock(self.root, locks, "_library.lock", LOCK_TIMEOUT):
                # Both scans are fresh: an id is allocated and a similarity decided
                # from files read here, never from a cache.
                discovery = self.discover(fresh=True)
                # No id is allocated and no absence decision is made from a scan that
                # missed records or subtrees (spec 4.4, TOOLS.md create C1).
                _require_complete_inventory(self.root, discovery)
                warnings = self.base_warnings()
                owner = None
                if kind == "project":
                    owner = _require_client(client, discovery)
                    if owner.status not in ("active", None):
                        warnings.append({"code": "record_invalid", "path": owner.relpath,
                                         "message": f"Client '{owner.id}' is not active."})
                candidates = _similar(discovery, kind, client, name, clean.get("aliases", []))
                if candidates and not allow_similar:
                    raise EngagementsError(
                        "similar_exists",
                        f"A {kind} named like '{name}' already exists. Use it, or pass allow_similar to create another.",
                        {"candidates": candidates})
                taken = {e.id for e in discovery.entries if e.id is not None}
                identifier = derive_id(name, kind, taken)
                data = _render(kind, identifier, name, client, clock, profile, source, clean, items)
                relpath = self._publish(kind, name, identifier, owner, data, locks)
            return CreateResult(identifier, kind, relpath, rev(data), candidates if allow_similar else [], warnings)
        except _Refusal as exc:
            raise _not_real_failure(self.root, exc) from None
        except OSError as exc:
            raise _io_error(self.root, exc) from exc

    def _publish(self, kind, name, identifier, owner, data, locks):
        """Place the record through held directories, including ``locks``.

        Only the exclusive-create fallback takes the record lock, after the
        library lock. The writer takes no library lock, so there is no lock inversion.
        """
        with self.open_root() as top:
            if kind == "client":
                return self._publish_client(top, name, identifier, data, locks)
            parts = PurePosixPath(owner.relpath).parent.parts
            ensure_inside(self, self.root.joinpath(*parts))
            client_dir = top
            try:
                for part in parts:
                    child = client_dir.open_dir(part)
                    if client_dir is not top:
                        client_dir.close()
                    client_dir = child
                    if client_dir is None:
                        raise _io_failure(self.root, self.root.joinpath(*parts), "not_a_directory")
                return self._publish_project(client_dir, name, identifier, data, locks)
            finally:
                if client_dir is not None and client_dir is not top:
                    client_dir.close()

    def _publish_client(self, top, name, identifier, data, locks):
        refused = set()
        for _ in range(_MAX_ATTEMPTS):
            stem = derive_filename(name, identifier, set(_listdir(top)) | refused)
            ensure_inside(self, self.root / stem)
            try:
                target = top.mkdir(stem)
            except FileExistsError:
                refused.add(stem)
                continue
            try:
                # Created and held in one step, without following a link: if the new
                # directory was swapped for a link it is refused, not written through.
                with target:
                    lock = _held_lock(self.root, locks, f"{identifier}.lock", LOCK_TIMEOUT)
                    if not _publish_exclusive(target, "Client.md", data, fallback_lock=lock):
                        raise _io_failure(self.root, self.root / stem / "Client.md", "exists")
            except BaseException:
                with contextlib.suppress(OSError):
                    top.rmdir(stem)
                raise
            return f"{stem}/Client.md"
        raise _io_failure(self.root, self.root, "no_free_name")

    def _publish_project(self, client_dir, name, identifier, data, locks):
        refused = set()
        projects, made = self._projects_dir(client_dir)
        try:
            # The cleanup below runs after this block closes the directory: Windows cannot remove a held one.
            with projects:
                for _ in range(_MAX_ATTEMPTS):
                    taken = {n[:-3] for n in _listdir(projects) if n.casefold().endswith(".md")} | refused
                    stem = derive_filename(name, identifier, taken)
                    ensure_inside(self, self.root / projects.relpath / f"{stem}.md")
                    lock = _held_lock(self.root, locks, f"{identifier}.lock", LOCK_TIMEOUT)
                    if _publish_exclusive(projects, f"{stem}.md", data, fallback_lock=lock):
                        return f"{projects.relpath}/{stem}.md"
                    refused.add(stem)
                raise _io_failure(self.root, self.root / projects.relpath, "no_free_name")
        except BaseException:
            if made:
                with contextlib.suppress(OSError):
                    client_dir.rmdir(PurePosixPath(projects.relpath).name)
            raise

    def _projects_dir(self, client_dir):
        """The client's Projects directory (TOOLS.md C6): reuse one real match, create when none.

        Returns the held directory and whether this call created it.
        """
        for _ in range(3):
            matches = [n for n in _listdir(client_dir) if _fold(n) == "projects"]
            if not matches:
                try:
                    held = client_dir.mkdir("Projects")
                except FileExistsError:
                    continue
                except BaseException:
                    with contextlib.suppress(OSError):
                        client_dir.rmdir("Projects")
                    raise
                return held, True
            if len(matches) > 1:
                raise _io_failure(self.root, self.root / client_dir.relpath / "Projects",
                                  "ambiguous_projects_directory")
            held = client_dir.open_dir(matches[0])
            if held is None:
                continue
            return held, False
        raise _io_failure(self.root, self.root / client_dir.relpath / "Projects", "unstable")


def _ambiguous(entries):
    ordered = sorted(entries, key=lambda e: (normalize_text(e.name or ""), e.id or "", e.relpath))
    return EngagementsError("ambiguous", "More than one record matches.",
                            {"candidates": [e.candidate() for e in ordered]})


def _match(entry, needle):
    """Best tier (3 exact, 2 prefix, 1 substring; 0 none) and the field that produced it."""
    if not needle:
        return 1, None
    fields = [("id", entry.id), ("name", entry.name), *[("alias", a) for a in entry.aliases]]
    best, match = 0, None
    for field_name, value in fields:
        if value is None:  # a listed record may have an unreadable name
            continue
        normalized = normalize_text(value)
        if normalized == needle:
            tier = 3
        elif normalized.startswith(needle):
            tier = 2
        elif needle in normalized:
            tier = 1
        else:
            continue
        if tier > best:
            best, match = tier, {"field": field_name, "value": value}
    return best, match


def _require_client(client, discovery):
    matches = [e for e in discovery.entries if e.id == client]
    if not matches or not any(e.kind == "client" for e in matches):
        raise EngagementsError("not_found", f"No client with id '{client}'.",
                               {"reason": "no_match", "field": "client"})
    if len(matches) > 1:
        raise _ambiguous(matches)
    return matches[0]


def _similar(discovery, kind, client, name, aliases):
    proposed = {normalize_text(x) for x in [name, *aliases]} - {""}
    found = []
    for entry in discovery.entries:
        if entry.kind != kind or entry.id is None or entry.name is None:
            continue
        if kind == "project" and entry.client != client:
            continue
        existing = {normalize_text(x) for x in [entry.name, *entry.aliases]} - {""}
        if proposed & existing:
            found.append(entry)
    found.sort(key=lambda e: (normalize_text(e.name), e.id, e.relpath))
    return [e.candidate() for e in found]
