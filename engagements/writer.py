"""Revision-checked writer: snapshots, rebase, conflicts, history (spec 5.1, 5.3).

Every public function takes the library root and a resolved record path, so it
does not depend on discovery. ``root`` is an opened ``Library`` or the
canonical root path it was opened at (``Library.root``); it is never resolved
again here (spec 4.1). Its last component is not followed, its pathname must
still be canonical, and with a ``Library`` its identity must match the one
recorded when it was opened, so a replaced root is ``io_error`` /
``root_changed``.

Every read and write below the root goes through directories held open by
descriptor (by handle on Windows), each opened without following a link: the
record's directory, ``locks``, ``history/<id>`` and ``snapshots``. A directory
swapped for a symlink after it was validated cannot
redirect a write. A platform with neither mechanism fails closed: every write
returns ``io_error`` / ``unsupported_platform`` and reads still work.

Time is injected through a ``clock`` callable that returns an aware datetime;
nothing here reads the system clock. Locks are ``fcntl.flock`` (``msvcrt`` on
Windows) on a descriptor opened per acquisition and a fresh ``FileLock`` is built for every
acquisition, so no lock state outlives a call or is shared between threads
(spec A2).
"""

import contextlib
import errno
import os
import re
import stat
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from datetime import UTC, timedelta
from pathlib import Path

from . import records
from .errors import EngagementsError
from .fsutil import (
    ROOT_CHANGED,
    FileLock,
    _Dir,
    _NotRealDirectory,
    _Refusal,
    _require_safe_writes,
    atomic_write_at,
    rev,
    section_key,
    write_temp_at,
)
from .redact import redact_label

LOCK_TIMEOUT = 10.0
HISTORY_KEEP = 50
SNAPSHOT_TTL = timedelta(days=14)
MAX_RECORD_BYTES = 1048576
# A writer holds a temp file for the length of one write, far less than a second.
# An hour is the conservative floor below which nothing is even reported, so a
# write in flight is not mistaken for a leftover.
TEMP_MIN_AGE = timedelta(hours=1)
_REV = re.compile(r"[0-9a-f]{16}")
_ID = re.compile(r"[a-z0-9][a-z0-9-]{1,62}")
# The plugin's own temp names: ``write_temp_at`` and the exclusive-create publish.
_TEMP_NAME = re.compile(r"\.engagements-(?:new-)?[0-9a-f]{16}\.tmp")
_ABSENT = "absent"
_INVALID = "invalid"
_TOO_LARGE = "too_large"
_MAX_WALK_DEPTH = 32
_LIBRARY_DEFECTS = {"duplicate_id", "unknown_client"}


@dataclass
class UpdateResult:
    """``data`` is the TOOLS.md ``engagements_update`` Data object."""

    data: dict
    warnings: list = field(default_factory=list)


# ---- errors and paths ---------------------------------------------------------


def _warning(code, message, path=None):
    result = {"code": code, "message": message}
    if path is not None:
        result["path"] = path
    return result


def _display(root, path):
    """A path for messages: relative to the root when inside it, never absolute."""
    try:
        return Path(path).relative_to(root).as_posix()
    except ValueError:
        return Path(path).name


def _io_error(root, path, cause=None):
    error = EngagementsError("io_error", "Filesystem operation failed.", {"path": _display(root, path)})
    error.__cause__ = cause
    return error


def _refused(root, exc, message="Filesystem operation failed."):
    return EngagementsError("io_error", message, {"path": exc.relpath or ".", "reason": exc.reason})


@contextlib.contextmanager
def _guard(root, path):
    """Map a raw OSError or a refusal to ``io_error`` and keep absolute paths out of details."""
    try:
        yield
    except _Refusal as exc:
        raise _refused(root, exc) from None
    except OSError as exc:
        raise _io_error(root, path, exc) from exc
    except EngagementsError as exc:
        details = exc.details
        if isinstance(details, dict) and isinstance(details.get("path"), str) and os.path.isabs(details["path"]):
            exc.details = {**details, "path": _display(root, details["path"])}
        raise


def _not_found(reason, path):
    return EngagementsError("not_found", "No record at that path.", {"reason": reason, "path": path})


def _root_changed():
    return EngagementsError("io_error", "The library root changed since it was opened.",
                            {"path": ".", "reason": ROOT_CHANGED})


def _root_of(root):
    """``(path, identity)`` for an opened ``Library`` or the canonical root path it was opened at."""
    if isinstance(root, (str, os.PathLike)):
        return Path(os.path.abspath(os.fspath(root))), None
    return Path(os.path.abspath(os.fspath(root.root))), getattr(root, "root_id", None)


@contextlib.contextmanager
def _opened_root(root, *, writable=True):
    """Hold the library root for one call: ``(root_path, top)``.

    The root is never resolved again (spec 4.1). Its last component is opened
    without following a link, its pathname must still be canonical (a swapped
    ancestor makes it resolve elsewhere), and a ``Library``'s recorded identity
    must match. Anything else is ``io_error`` / ``root_changed``. ``writable`` is
    for a caller that only reads, so it holds no write access on Windows either.
    """
    path, identity = _root_of(root)
    try:
        top = _Dir.open_root(path, identity, writable)
    except _Refusal:
        raise _root_changed() from None
    except OSError as exc:
        raise _io_error(path, path, exc) from exc
    with top:
        if os.path.realpath(path) != os.fspath(path):
            raise _root_changed()
        yield path, top


def _resolve(root, path):
    """Validate ``path`` for a read or write: inside the root, not excluded.

    ``root`` is the held canonical root. Returns ``(parts, name, relative)``:
    the record directory's components below the root, the file name and the
    slash path. Mirrors spec 4.1: the destination's parent must be inside the
    root (``outside_root``), and metadata, dot-directories and symlinked
    components are never records. The caller then opens ``parts`` one held
    directory at a time, so a component swapped after this check is refused
    rather than followed.
    """
    root = Path(root)
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = Path(os.path.normpath(candidate))
    parent_real = Path(os.path.realpath(candidate.parent))
    if parent_real != root and root not in parent_real.parents:
        raise EngagementsError("outside_root", "Path is outside the library root.", {"path": candidate.name})
    real_parts = parent_real.relative_to(root).parts
    with contextlib.suppress(ValueError):
        if candidate.parent.relative_to(root).parts != real_parts:
            raise _not_found("excluded", "/".join((*real_parts, candidate.name)))
    relative = "/".join((*real_parts, candidate.name))
    # Dot-directories are excluded (spec 4.1); a dot-prefixed file name is not a
    # dot-directory, so only the directory components are checked here.
    if any(part.startswith(".") for part in real_parts) or (not real_parts and candidate.name == ".engagements"):
        raise _not_found("excluded", relative)
    if not candidate.name.endswith(".md"):  # TOOLS.md "Filesystem boundary": .md regular files only
        raise _not_found("not_a_record", relative)
    return real_parts, candidate.name, relative


def _open_record_dir(root, top, parts, relative):
    """The record's directory, held: opened from the root one component at a time, never through a link.

    The root handle is duplicated inside the error mapping, so a descriptor
    failure (EMFILE) is ``io_error`` like any other.
    """
    current = None
    try:
        with _guard(root, Path(root) / relative):
            current = top.dup()
            for part in parts:
                try:
                    child = current.open_dir(part)
                except _NotRealDirectory:
                    raise _not_found("excluded", relative) from None
                if child is None:
                    raise _not_found("no_match", relative)
                current.close()
                current = child
    except BaseException:
        if current is not None:
            current.close()
        raise
    return current


_READ_FLAGS = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
               | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0))


def _read(root, directory, name, relative):
    """Read a regular, non-symlink file of at most 1 MiB: ``(bytes, mode)``.

    The file is opened relative to its held directory. The open never follows a
    final symlink and never blocks (a FIFO swapped in for the record would
    otherwise wait for a writer); the type is checked on the descriptor before
    anything is read.
    """
    with _guard(root, Path(root) / relative):
        try:
            fd = directory.open(name, _READ_FLAGS)
        except FileNotFoundError:
            raise _not_found("no_match", relative) from None
        except OSError as exc:
            if exc.errno == errno.ELOOP:  # a symlink swapped in at the final component
                raise _not_found("excluded", relative) from None
            raise
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise _not_found("excluded", relative)
            if info.st_size > MAX_RECORD_BYTES:
                raise _not_found("not_a_record", relative)
            chunks = []
            remaining = MAX_RECORD_BYTES + 1
            while remaining:
                chunk = os.read(fd, min(remaining, 1 << 20))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
        finally:
            os.close(fd)
    data = b"".join(chunks)
    if len(data) > MAX_RECORD_BYTES:
        raise _not_found("not_a_record", relative)
    return data, stat.S_IMODE(info.st_mode)


def _revision_of(root, directory, name, relative):
    """The target's current rev, or ``None`` when it is no longer a record file.

    ``None`` covers a target that was deleted, replaced by a symlink or a
    non-regular file, or grown past 1 MiB: each is a concurrent change. Any
    other filesystem failure is not evidence of a change and propagates as
    ``io_error`` (TOOLS.md stage 8).
    """
    try:
        return rev(_read(root, directory, name, relative)[0])
    except EngagementsError as exc:
        if exc.code == "not_found":
            return None
        raise


# ---- library metadata ----------------------------------------------------------


def _meta_dir(top, *parts):
    """``.engagements/<parts>`` as held real directories, created on demand.

    TOOLS.md "Filesystem boundary": before writing metadata the plugin verifies
    that ``.engagements`` and the subdirectory are real directories, not
    symlinks. Each one is opened without following a link relative to its held
    parent, and the caller writes relative to the returned handle, so a
    directory swapped after this check cannot redirect the write.
    A violation is an ``io_error`` here and the caller decides the consequence
    (metadata failure policy). The caller closes the returned ``_Dir``.

    Duplicating the root handle is inside that mapping too, so a descriptor
    failure is an ``io_error`` the caller's stage policy handles, never a raw
    ``OSError``.
    """
    current = None
    try:
        current = top.dup()
        for part in (".engagements", *parts):
            child = current.ensure_dir(part)
            current.close()
            current = child
    except BaseException as exc:
        if current is not None:
            current.close()
        if isinstance(exc, _NotRealDirectory):
            raise EngagementsError("io_error", "Library metadata is not a real directory.",
                                   {"path": exc.relpath, "reason": exc.reason}) from None
        if isinstance(exc, _Refusal):
            raise _refused(top.path, exc) from None
        if isinstance(exc, OSError):
            raise _io_error(top.path, exc.filename or top.path, exc) from exc
        raise
    return current


def _existing_meta_dir(top, *parts):
    """Held ``.engagements/<parts>`` when every level is a real directory; None when absent.

    Never creates anything. Raises ``_Refusal`` for a symlink or other
    non-directory, and ``OSError`` (including from duplicating the root) for
    the caller to map.
    """
    current = None
    try:
        current = top.dup()
        for part in (".engagements", *parts):
            child = current.open_dir(part)
            current.close()
            current = child
            if current is None:
                return None
    except BaseException:
        if current is not None:
            current.close()
        raise
    return current


def _mtime_at(directory, name):
    """A regular file's mtime here, from a descriptor that never follows a link; ``None`` otherwise."""
    try:
        fd = directory.open(name, _READ_FLAGS)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return None
        return info.st_mtime
    except OSError:
        return None
    finally:
        os.close(fd)


def _snapshot_at(directory, name, revision):
    """``(bytes, None)`` for a usable snapshot; ``(None, reason)`` for one that cannot be used.

    A usable snapshot is a regular file of at most the record size limit whose
    hash matches the revision in its name. The reasons are ``absent`` (no such
    entry), ``too_large`` (a snapshot is never larger than a record can be) and
    ``invalid`` (not a regular file, behind a link, unreadable, or a hash that
    does not match). The size comes from the descriptor before a byte is read, so
    an oversized or sparse file is refused without being read into memory whole
    (spec 5.1).
    """
    try:
        fd = directory.open(name, _READ_FLAGS)
    except FileNotFoundError:
        return None, _ABSENT
    except OSError:
        return None, _INVALID
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return None, _INVALID
        if info.st_size > MAX_RECORD_BYTES:
            return None, _TOO_LARGE
        chunks = []
        remaining = MAX_RECORD_BYTES + 1
        while remaining:
            chunk = os.read(fd, min(remaining, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    except OSError:
        return None, _INVALID
    finally:
        os.close(fd)
    data = b"".join(chunks)
    if len(data) > MAX_RECORD_BYTES:  # grew while it was being read
        return None, _TOO_LARGE
    return (data, None) if rev(data) == revision else (None, _INVALID)


def _load_snapshot_at(top, revision):
    """``(bytes, problem)`` for ``revision``'s snapshot, read without following a link."""
    try:
        directory = _existing_meta_dir(top, "snapshots")
    except (OSError, _Refusal):
        return None, _ABSENT
    if directory is None:
        return None, _ABSENT
    with directory:
        return _snapshot_at(directory, f"{revision}.md", revision)


def load_snapshot(root, revision):
    """The bytes snapshotted under ``revision``, or ``None`` when absent or unusable.

    A snapshot whose hash does not match its name, or that is larger than a
    record may be, counts as absent (spec 5.1). A malformed revision, a symlinked
    metadata directory, a changed root or any read failure also count as absent,
    so callers see one outcome: ``base_unavailable``.
    """
    if not isinstance(revision, str) or not _REV.fullmatch(revision):
        return None
    try:
        with _opened_root(root) as (_, top):
            return _load_snapshot_at(top, revision)[0]
    except EngagementsError:
        return None


def _write_snapshot(root, top, data, revision, clock):
    """Atomically snapshot ``data`` under ``revision``; skip when a usable one exists.

    A snapshot the read could not use (wrong hash, larger than a record may be) is
    replaced. Returns the failure of the check that runs after the file is
    published, if the platform reported one (see ``fsutil.atomic_write_at``); the
    snapshot exists either way.

    With a clock, the file's mtime is set from it, so every snapshot is dated by
    the injected time, and a re-read of a still-needed revision refreshes that date.
    """
    name = f"{revision}.md"
    with _meta_dir(top, "snapshots") as directory, _guard(root, Path(root) / ".engagements" / "snapshots" / name):
        _require_safe_writes(directory._child_rel(name))
        problem = None
        if _snapshot_at(directory, name, revision)[0] is None:
            problem = atomic_write_at(directory, name, data)
        if clock is not None:
            directory.set_mtime(name, clock().timestamp())
    return problem


def prune_snapshots(root, *, clock):
    """Delete snapshots last written or read more than 14 days before ``clock()``."""
    cutoff = (clock() - SNAPSHOT_TTL).timestamp()
    with _opened_root(root) as (path, top):
        display = path / ".engagements" / "snapshots"
        try:
            directory = _existing_meta_dir(top, "snapshots")
        except _Refusal:
            return  # a symlink the plugin must never follow
        except OSError as exc:
            raise _io_error(path, display, exc) from exc
        if directory is None:
            return
        with directory, _guard(path, display), directory.scandir() as entries:
            for entry in entries:
                if entry.name.endswith(".md") and entry.is_file(follow_symlinks=False) \
                        and entry.stat(follow_symlinks=False).st_mtime < cutoff:
                    with contextlib.suppress(FileNotFoundError):
                        directory.unlink(entry.name)


def read_record(root, path, *, clock=None, warnings=None):
    """Read a record and snapshot its exact bytes: ``(bytes, rev)`` (spec 5.1).

    The rev covers the complete bytes whatever the caller then shows of them.
    A snapshot failure never fails the read: it appends ``snapshot_failed`` to
    ``warnings`` (a list the caller supplies) and returns normally, and a later
    stale update from this rev gets ``base_unavailable``. With ``clock`` the
    read also dates the snapshot and prunes snapshots older than 14 days.
    """
    sink = warnings if warnings is not None else []
    with _opened_root(root) as (root_path, top):
        parts, name, relative = _resolve(root_path, path)
        with _open_record_dir(root_path, top, parts, relative) as directory:
            data, _ = _read(root_path, directory, name, relative)
        revision = rev(data)
        try:
            problem = _write_snapshot(root_path, top, data, revision, clock)
        except EngagementsError:
            sink.append(_warning("snapshot_failed", "The read snapshot could not be written; a later stale update "
                                 "from this revision cannot be rebased.", relative))
            return data, revision
        if problem is not None:
            sink.append(_warning("maintenance_warning", "The snapshot's folder could not be re-checked after the "
                                 "write; it holds no record.", relative))
    if clock is not None:
        try:
            prune_snapshots(root, clock=clock)
        except EngagementsError:
            sink.append(_warning("maintenance_warning", "Old snapshots could not be pruned.", relative))
    return data, revision


# ---- the update transaction ------------------------------------------------------


def _defect_error(root_relative, defects):
    return EngagementsError("malformed", "Record has defects; run doctor.",
                            {"path": root_relative, "defects": [{"kind": kind} for kind in sorted(defects)]})


def _effective_defects(record, kind, library_defects, client_exists):
    """Global defects: parsed ones, the injected library-wide ones, and ``unknown_client``."""
    external = set(library_defects(record)) if library_defects is not None else set()
    if external - _LIBRARY_DEFECTS:
        raise ValueError("library_defects may only return duplicate_id and unknown_client")
    if record.kind == "project":
        client = (record.properties or {}).get("client")
        if isinstance(client, str) and not client_exists(client):
            external.add("unknown_client")
    return set(record.global_defects) | external


def _check_defects(relative, defects, kind, ops, client_exists):
    """Any global defect is ``malformed``, except the C5 repair of ``unknown_client``.

    The same rule ``apply_ops`` enforces on the locked bytes; it is applied here
    first so the pre-lock stage 4 and the locked stage 4 agree on precedence.
    """
    if not defects:
        return
    repair = (defects == {"unknown_client"} and kind == "project"
              and all(op["op"] == "set_property" and op["key"] == "client" and client_exists(op["value"])
                      for op in ops))
    if not repair:
        raise _defect_error(relative, defects)


def _identity_footprints(pre, locked):
    """Footprints of ``engagements`` and ``id`` that differ between the two reads."""
    found = []
    pre_record, locked_record = records.parse(pre), records.parse(locked)
    for key, before, after in (("engagements", pre_record.kind, locked_record.kind), ("id", pre_record.id, locked_record.id)):
        if before != after:
            op = {"op": "set_property", "key": key}
            # SPEC-QUESTION: TOOLS.md says an identity change is footprint_changed "with that property as the
            # footprint" but footprints carry an op_index and no op names these keys; op_index is null here.
            found.append({"op_index": None, "type": "property", "key": key,
                          "base_text": records.footprint(pre, op), "current_text": records.footprint(locked, op)})
    return found


def _identity_conflict(kind, expected_id, current):
    """``conflict`` / ``footprint_changed`` when the bytes no longer carry the resolved identity.

    A handler resolves an id against discovery, then hands the
    writer only a path. Between the two the path can hold a different record, and
    without this check the writer would silently update that one. ``current`` is
    the parse of the bytes just read, so the footprint text is theirs.
    """
    changed = []
    if current.kind != kind:
        changed.append(("engagements", f"engagements: {kind}\n"))
    if expected_id is not None and current.id != expected_id:
        changed.append(("id", f"id: {expected_id}\n"))
    return _conflict("footprint_changed", "The record's identity changed since it was resolved. Nothing was "
                     "written. Show the user both versions.", rev(current.data),
                     [{"op_index": None, "type": "property", "key": key, "base_text": base,
                       "current_text": records.footprint(current.data, {"op": "set_property", "key": key})}
                      for key, base in changed])


def _op_target(op):
    if op["op"] == "set_property":
        return "property", op["key"]
    return "section", section_key(op["section"])


def _changed_footprints(ops, base, current):
    """Every op footprint that differs, all taken on the untouched base and current."""
    cache = {}

    def taken(side, data, op):
        key = (side, *_op_target(op))
        if key not in cache:
            cache[key] = records.footprint(data, op)
        return cache[key]

    changed = []
    for index, op in enumerate(ops):
        before, after = taken("base", base, op), taken("current", current, op)
        if before != after:
            kind, key = _op_target(op)
            changed.append({"op_index": index, "type": kind, "key": key, "base_text": before, "current_text": after})
    return changed


def _conflict(reason, message, current_rev, footprints=()):
    return EngagementsError("conflict", message,
                            {"reason": reason, "current_rev": current_rev, "footprints": list(footprints)})


def _base_unavailable_message(problem):
    """``base_unavailable``'s message, naming an unusable snapshot for what it is."""
    if problem == _TOO_LARGE:
        return ("The snapshot of the revision you read is larger than a record may be, so it cannot be used to "
                "compare against. Nothing was written. Read the record again.")
    return ("The revision you read is no longer available to compare against. Nothing was written. "
            "Read the record again.")


def _change_log_line(data):
    log = next(section for section in records.parse(data).sections if section.key == "change log")
    lines = [line for line in re.split(r"\r\n|\n", data[log.body_start:log.end].decode("utf-8")) if line.strip()]
    return lines[-1]


def _fsync_dir(directory):
    """Flush a held directory's entries where the platform supports it (best effort).

    Windows has no directory sync; there the entry changes are made write-through
    (``MOVEFILE_WRITE_THROUGH``) and this does nothing.
    """
    if directory.fd is None:  # Windows handle-held directories, and platforms without dir_fd
        return
    try:
        os.fsync(directory.fd)
    except OSError as exc:
        if exc.errno not in (errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP):  # directories cannot be synced here
            raise


def _write_history(root, top, record_id, stamp, current_rev, data):
    """The required pre-update copy (spec 5.3 step 5). Any failure is ``io_error`` before the replace.

    The directory sync is checked: only a platform that cannot sync a
    directory is ignored, so a real error such as EIO aborts the update with
    the record untouched.

    Returns the failure of the check that ran after the copy was published, if
    the platform reported one: the copy exists by then, so the update goes on and
    that failure is the caller's ``maintenance_warning`` (see ``fsutil.atomic_write_at``).
    """
    name = f"{stamp}-{current_rev}.md"
    with _meta_dir(top, "history", record_id) as directory, \
            _guard(root, Path(root) / ".engagements" / "history" / record_id / name):
        problem = atomic_write_at(directory, name, data)
        _fsync_dir(directory)
    return problem


def _prune_history(root, top, record_id):
    with _meta_dir(top, "history", record_id) as directory, \
            _guard(root, Path(root) / ".engagements" / "history" / record_id):
        with directory.scandir() as entries:
            names = sorted(e.name for e in entries if e.name.endswith(".md") and e.is_file(follow_symlinks=False))
        for name in names[:-HISTORY_KEEP]:
            with contextlib.suppress(FileNotFoundError):
                directory.unlink(name)


# ---- temp files a crash left behind: reported, never removed (spec 5.1) --------


def _walk_dirs(directory, parts, depth):
    """Yield ``(parts, held directory)`` for ``directory`` and every real directory below it.

    Each level is opened from its held parent without following a link, so a
    symlinked directory is skipped rather than entered and nothing outside the
    held root can be reached. ``.git`` is not walked: it is not the plugin's
    business. The depth bound stops a self-referential tree.
    """
    yield parts, directory
    if depth >= _MAX_WALK_DEPTH:
        return
    try:
        with directory.scandir() as entries:
            names = sorted(entry.name for entry in entries if entry.is_dir(follow_symlinks=False))
    except OSError:
        return
    for name in names:
        if name == ".git":
            continue
        try:
            child = directory.open_dir(name)
        except (OSError, _Refusal):
            continue
        if child is None:
            continue
        with child:
            yield from _walk_dirs(child, (*parts, name), depth + 1)


def _temp_candidates(directory, cutoff):
    """The plugin's own temp files here whose dates are older than ``cutoff``, with those dates."""
    try:
        with directory.scandir() as entries:
            names = sorted(entry.name for entry in entries
                           if _TEMP_NAME.fullmatch(entry.name) and entry.is_file(follow_symlinks=False))
    except OSError:
        return []
    stale = []
    for name in names:
        stamp = _mtime_at(directory, name)
        if stamp is not None and stamp < cutoff:
            stale.append((name, stamp))
    return stale


def _relative_name(parts, name):
    return "/".join((*parts, name)) if parts else name


def stale_temp_files(root, *, clock, minimum_age=TEMP_MIN_AGE):
    """The plugin's own temp files older than ``minimum_age``, as ``(relative path, age in seconds)``.

    Read-only: nothing is created, locked or deleted, so ``doctor`` can report
    what an interrupted write left behind (spec 6.6). Only the names the plugin
    itself writes are considered, and a symlinked entry is never one of them.
    Nothing removes these files automatically: a temp file that is merely old is
    not evidence that no writer holds it (spec 5.1).
    """
    cutoff = (clock() - minimum_age).timestamp()
    now = clock().timestamp()
    found = []
    with _opened_root(root, writable=False) as (path, top):
        for parts, directory in _walk_dirs(top, (), 0):
            for name, stamp in _temp_candidates(directory, cutoff):
                found.append((_relative_name(parts, name), max(0, int(now - stamp))))
    return sorted(found)


def _commit(root, directory, name, data, mode, expected_rev, before_hook, after_hook, relative):
    """Spec 5.3 step 6: temp file, fsync, re-hash, ``os.replace``.

    Every step is relative to the record's held directory, so the temp file, the
    re-hash and the replace all address the directory that was validated.

    Returns the problems found after the replace, as messages for the caller's
    warnings, and an empty list when there are none. The replace publishes the
    new bytes: a check that fails after it cannot undo that, and an error here
    would report a committed write as failed, so a retry would duplicate it.
    """
    display = Path(root) / relative
    temporary = None
    after = None
    problems = []
    try:
        with _guard(root, display):
            temporary = write_temp_at(directory, data, mode)
        if before_hook is not None:
            before_hook()
        observed_rev = _revision_of(root, directory, name, relative)
        if observed_rev != expected_rev:
            raise _conflict("changed_during_write",
                            "The record changed while the update was being written. Nothing was written. "
                            "Show the user both versions.", observed_rev)
        if after_hook is not None:
            after_hook()
        with _guard(root, display):
            after = directory.replace(temporary, name)
        temporary = None
    finally:
        if temporary is not None:
            with contextlib.suppress(OSError, _Refusal):
                directory.unlink(temporary)
    if after is not None:
        problems.append("The record's directory could not be re-checked after the write.")
    try:
        _fsync_dir(directory)
    except OSError:
        problems.append("The directory could not be synced after the write.")
    return problems


def _validate_arguments(base_rev, source, ops, kind, profile):
    if kind not in ("client", "project"):
        raise ValueError("kind must be 'client' or 'project'")
    if profile is not None and not records.is_single_line(profile, 200):
        raise ValueError("profile must be a single-line string or None")
    # Stage 1, in the schema's property order: base_rev, source, ops.
    if not isinstance(base_rev, str) or not _REV.fullmatch(base_rev):
        raise EngagementsError("invalid_op", "Invalid operation.", {"field": "base_rev", "reason": "invalid_value"})
    if not records.is_single_line(source, 200):
        raise EngagementsError("invalid_op", "Invalid operation.", {"field": "source", "reason": "invalid_value"})
    records.validate_shapes(ops)


def update(root, path, base_rev, source, ops, *, kind, profile, clock, client_exists,
           library_defects: Callable[[records.Record], Collection[str]] | None = None,
           expected_id: str | None = None,
           lock_timeout=LOCK_TIMEOUT, before_final_rehash=None, after_final_rehash=None):
    """Apply ``ops`` to one record in one revision-checked transaction.

    Implements spec 5.3 steps 1 to 7 with TOOLS.md "Error precedence" stages 1
    and 3 to 8 and "Footprints and rebase". ``root`` is an opened ``Library`` or
    its canonical root path (see the module docstring). ``path`` is the
    resolved record path and ``kind`` its resolved kind. ``expected_id`` is the
    id the caller resolved for that path: the bytes must still carry both, at
    the pre-lock read and again under the lock, or nothing is written.
    ``library_defects(record)``
    is how discovery supplies the library-wide ``duplicate_id`` and
    ``unknown_client`` facts; it is called with the parsed record for the
    pre-lock bytes and again for the locked bytes, so it must not mutate
    anything. ``unknown_client`` is also derived from ``client_exists`` for a
    project's own ``client``.

    Test hooks take no arguments: ``before_final_rehash`` runs after the temp
    file is fsynced and before the target is re-hashed, ``after_final_rehash``
    between the re-hash and ``os.replace`` (the window spec 5.5 admits).

    Returns an ``UpdateResult``. Failures are ``EngagementsError``; a failure
    after the replace becomes a ``maintenance_warning`` in the result.
    """
    _validate_arguments(base_rev, source, ops, kind, profile)
    warnings = []
    with _opened_root(root) as (root_path, top):
        with _guard(root_path, root_path):
            _require_safe_writes()  # no write where it could be redirected
        parts, name, relative = _resolve(root_path, path)
        with _open_record_dir(root_path, top, parts, relative) as directory:
            record_id, outcome = _update_held(root_path, top, directory, name, relative, base_rev, source, ops,
                                              kind, expected_id, profile, clock, client_exists, library_defects,
                                              lock_timeout, before_final_rehash, after_final_rehash, warnings)
        new_data, new_rev, rebased = outcome
        try:
            snapshot_problem = _write_snapshot(root_path, top, new_data, new_rev, clock)
        except EngagementsError:
            warnings.append(_warning("maintenance_warning", "The new revision could not be snapshotted; a later stale "
                                     "update from it cannot be rebased.", relative))
        else:
            if snapshot_problem is not None:
                warnings.append(_warning("maintenance_warning", "The snapshot's folder could not be re-checked after "
                                         "the write; the record itself was committed.", relative))
    try:
        prune_snapshots(root, clock=clock)
    except (OSError, EngagementsError):
        warnings.append(_warning("maintenance_warning", "Old snapshots could not be pruned.", relative))
    return UpdateResult(
        {"id": record_id, "path": relative, "new_rev": new_rev, "rebased": rebased, "applied": len(ops),
         "change_log_line": _change_log_line(new_data)},
        warnings)


def _update_held(root, top, directory, name, relative, base_rev, source, ops, kind, expected_id, profile, clock,
                 client_exists, library_defects, lock_timeout, before_hook, after_hook, warnings):
    """Stages 4 to 8 with the root and the record's directory held. Returns ``(record_id, outcome)``."""
    # Stage 4 on the pre-lock read: needed for the record id (the lock name).
    pre, _ = _read(root, directory, name, relative)
    pre_record = records.parse(pre)
    if not pre_record.is_record:
        raise _not_found("not_a_record", relative)
    defects = _effective_defects(pre_record, kind, library_defects, client_exists)
    _check_defects(relative, defects, kind, ops, client_exists)
    if not isinstance(pre_record.id, str) or not _ID.fullmatch(pre_record.id):
        raise _defect_error(relative, {"invalid_id"})
    if pre_record.kind != kind or (expected_id is not None and pre_record.id != expected_id):
        # The caller resolved a different identity than the file now holds. The path
        # may hold another record of the same kind: never apply to it.
        raise _identity_conflict(kind, expected_id, pre_record)
    records.validate_values(kind, ops, client_exists=client_exists)
    record_id = pre_record.id

    # Stage 5: the record lock, a fresh FileLock for this acquisition only, opened
    # relative to the held lock directory.
    lock_path = Path(root) / ".engagements" / "locks" / f"{record_id}.lock"
    with _meta_dir(top, "locks") as locks, _guard(root, lock_path):
        lock = FileLock(lock_path, timeout=lock_timeout, directory=locks)
        lock.__enter__()
    try:
        outcome = _locked_update(root, top, directory, name, relative, record_id, pre, pre_record, base_rev, source,
                                 ops, kind, expected_id, profile, clock, client_exists, library_defects, before_hook,
                                 after_hook, warnings)
    except BaseException:
        with contextlib.suppress(EngagementsError):
            lock.__exit__(None, None, None)  # the original error wins
        raise
    try:
        lock.__exit__(None, None, None)
    except EngagementsError:
        warnings.append(_warning("maintenance_warning", "The record lock could not be released cleanly.", relative))
    return record_id, outcome


def _locked_update(root, top, directory, name, relative, record_id, pre, pre_record, base_rev, source, ops, kind,
                   expected_id, profile, clock, client_exists, library_defects, before_hook, after_hook, warnings):
    """Everything that runs while the record lock is held (spec 5.3 steps 2 to 6)."""
    # Step 2 and stage 4 again, against the locked bytes, before the revision check.
    current, mode = _read(root, directory, name, relative)
    current_rev = rev(current)
    locked = records.parse(current)
    defects = set()
    if locked.is_record:
        defects = _effective_defects(locked, kind, library_defects, client_exists)
        _check_defects(relative, defects, kind, ops, client_exists)
    if (not locked.is_record or (locked.kind, locked.id) != (pre_record.kind, pre_record.id)
            or (expected_id is not None and locked.id != expected_id)):
        # The identity the caller resolved is checked again here, not only against the
        # pre-lock read, so a swap in the gap cannot redirect the write.
        raise _conflict("footprint_changed", "The record's identity changed since it was resolved. Nothing was "
                        "written. Show the user both versions.", current_rev, _identity_footprints(pre, current))
    # The rest of stage 4 again: value validation, including whether a proposed
    # client still exists, is repeated on the locked state before step 3.
    records.validate_values(kind, ops, client_exists=client_exists)

    # Step 3: revision check and footprint comparison.
    rebased = current_rev != base_rev
    if rebased:
        base, problem = _load_snapshot_at(top, base_rev)
        if base is None:
            raise _conflict("base_unavailable", _base_unavailable_message(problem), current_rev)
        changed = _changed_footprints(ops, base, current)
        if changed:
            # Keys are labels from the record: a flagged one is replaced whole before
            # it goes into the message.
            names = ", ".join(sorted({redact_label(item["key"]) for item in changed}))
            raise _conflict("footprint_changed", f"Changed since your read: {names}. Nothing was written. "
                            "Show the user both versions.", current_rev, changed)

    # Step 4 and TOOLS.md stage 7: apply everything in memory.
    try:
        new_data = records.apply_ops(current, kind, ops, clock=clock, profile=profile, source=source,
                                     library_defects=defects & _LIBRARY_DEFECTS,
                                     client_exists=client_exists)
    except EngagementsError as exc:
        if exc.code == "malformed" and isinstance(exc.details, dict) and "path" not in exc.details:
            exc.details = {"path": relative, **exc.details}
        raise

    # Step 5: the pre-update history copy. A failure here aborts before the replace.
    stamp = clock().astimezone(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    history_problem = _write_history(root, top, record_id, stamp, current_rev, current)
    if history_problem is not None:
        # The copy is published; only its directory re-check failed, so the update
        # goes on and the failure is reported as metadata, never as a failed write.
        warnings.append(_warning("maintenance_warning", "The history folder could not be re-checked after the copy "
                                 "was written; the record was not affected.", relative))

    # Step 6: temp file, fsync, re-hash, replace. The history entry is already written,
    # so its retention runs whatever happens next: a failed attempt that keeps writing
    # history must not grow the folder past its cap (spec 5.3 step 5).
    try:
        problems = _commit(root, directory, name, new_data, mode, current_rev, before_hook, after_hook, relative)
    finally:
        try:
            _prune_history(root, top, record_id)
        except (OSError, EngagementsError):
            warnings.append(_warning("maintenance_warning", "Old history entries could not be pruned.", relative))

    # From here the record is committed: every failure is a warning (metadata failure policy).
    for message in problems:
        warnings.append(_warning("maintenance_warning", message, relative))
    return new_data, rev(new_data), rebased
