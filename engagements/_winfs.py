"""Win32/NT primitives behind native Windows record writes (SPEC 5.3, TOOLS.md "Filesystem boundary").

CPython on Windows has no ``dir_fd`` and no ``O_NOFOLLOW``. This module is the
only place the plugin talks to the Win32 and NT APIs for that. It is stdlib
``ctypes``, imported only when ``sys.platform == "win32"``, and it knows nothing
about Engagements: ``fsutil._WinDir`` builds the directory seam on these
primitives.

What the primitives guarantee, and what the caller adds:

* A directory is opened by path once, with ``FILE_FLAG_OPEN_REPARSE_POINT`` and
  without ``FILE_SHARE_DELETE``, so while it is held it cannot be renamed,
  deleted or replaced. The access mask asks for ``FILE_ADD_FILE``,
  ``FILE_ADD_SUBDIRECTORY`` and ``FILE_DELETE_CHILD``. Write sharing is granted
  because the file system opens the directory for write itself when a hard link
  or a rename adds a name to it (withholding it fails both with WinError 32;
  native probe), and ``FSCTL_SET_REPARSE_POINT`` needs only
  ``FILE_WRITE_DATA`` or ``FILE_WRITE_ATTRIBUTES`` (MS-FSA 2.1.5.10.37), not
  ``DELETE``, so delete sharing is not what stops an in-place reparse mutation
  either. What confines a write is the next point: the operation is relative to
  the held object, so a reparse point on it cannot redirect the write.
* Every child operation is *object-relative*. Creates and opens are
  ``NtCreateFile`` with ``OBJECT_ATTRIBUTES.RootDirectory`` set to the held
  handle and exactly one name component; rename is
  ``NtSetInformationFile(FileRenameInformation)`` with ``RootDirectory``; hard
  link is ``FileLinkInformation`` with ``RootDirectory``; delete is
  ``FileDispositionInformation`` on an object-relative open. No pathname is
  built from a held handle and resolved again, so swapping a directory for a
  link (or replacing it) between the check and the write cannot redirect the
  write.
* A reparse point set on a held directory cannot redirect those operations
  either: the kernel refuses to resolve a relative name through a
  reparse-pointed parent (WinError 1921) and nothing is created outside.
  ``tests/test_windows_native.py`` proves that natively, because it is the
  property the no-outside-write contract rests on. The caller still re-checks
  the held directory's attributes before and after every child operation and
  fails closed with ``reparse_point``.
* ``FILE_OPEN_REPARSE_POINT`` opens a reparse point as itself instead of
  following it. Every child name is relative to a handle, so there is no
  intermediate component to traverse, and ``OBJ_DONT_REPARSE`` is not set: it
  refuses the name parse even at the final component, which would hide the
  entry's own attributes behind an unmapped status instead of letting the
  caller see and refuse the reparse point (native test for a
  dangling link). The caller refuses any opened entry carrying the reparse
  attribute.
* Every path passed to a pathname call (only the root is opened that way) is in
  the ``\\\\?\\`` form, so Win32 does no normalization: no ``..``, no trailing-dot
  trimming, no ``CON``/``NUL`` device names.
* Errors are ``OSError`` (``FileExistsError``, ``FileNotFoundError``,
  ``PermissionError`` and so on): an NTSTATUS is mapped through
  ``RtlNtStatusToDosError`` and ``ctypes.WinError``, so callers keep one error
  vocabulary on every platform.

Every function declares ``argtypes`` and ``restype``.
"""

import ctypes
import msvcrt
import ntpath
import time
from ctypes import wintypes

_ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)

HANDLE = ctypes.c_void_p
NTSTATUS = ctypes.c_long
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

# --- access rights -------------------------------------------------------------

FILE_READ_DATA = 0x0001             # FILE_LIST_DIRECTORY on a directory
FILE_WRITE_DATA = 0x0002            # FILE_ADD_FILE on a directory
FILE_APPEND_DATA = 0x0004           # FILE_ADD_SUBDIRECTORY on a directory
FILE_DELETE_CHILD = 0x0040
FILE_READ_ATTRIBUTES = 0x0080
FILE_WRITE_ATTRIBUTES = 0x0100
DELETE = 0x00010000
SYNCHRONIZE = 0x00100000
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000

# --- share modes ---------------------------------------------------------------

FILE_SHARE_READ = 0x1
FILE_SHARE_WRITE = 0x2
FILE_SHARE_DELETE = 0x4

# --- NtCreateFile dispositions ---------------------------------------------------

FILE_OPEN = 1
FILE_CREATE = 2
FILE_OPEN_IF = 3

# --- NtCreateFile options -------------------------------------------------------

FILE_DIRECTORY_FILE = 0x00000001
FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
FILE_NON_DIRECTORY_FILE = 0x00000040
FILE_OPEN_REPARSE_POINT = 0x00200000

# OBJECT_ATTRIBUTES.Attributes
OBJ_CASE_INSENSITIVE = 0x00000040

# FILE_INFORMATION_CLASS values used by NtSetInformationFile.
FILE_RENAME_INFORMATION = 10
FILE_LINK_INFORMATION = 11
FILE_DISPOSITION_INFORMATION = 13

# CreateFileW flags and attributes.
FILE_ATTRIBUTE_DIRECTORY = 0x10
FILE_ATTRIBUTE_NORMAL = 0x80
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000

# Dispositions (CreateFileW), kept for the pathname root open.
CREATE_NEW = 1
OPEN_EXISTING = 3

# An access profile for a held directory that can be written through.
WRITE_DIRECTORY_ACCESS = (FILE_READ_DATA | FILE_READ_ATTRIBUTES | FILE_WRITE_DATA
                          | FILE_APPEND_DATA | FILE_DELETE_CHILD | SYNCHRONIZE)
# A held directory used only for reading: no write access, so it never blocks
# another process, and the kernel refuses to create children through it.
READ_DIRECTORY_ACCESS = FILE_READ_DATA | FILE_READ_ATTRIBUTES | SYNCHRONIZE

# A held directory grants write sharing: the file system opens it for write
# itself when a hard link or a rename adds a name to it, and withholding write
# sharing fails those calls with WinError 32 (native probe). Delete
# sharing stays withheld, so the held directory itself can be neither renamed nor
# deleted while it is held. What confines a write is that every child operation
# is relative to this handle: a reparse point set on the held directory cannot
# redirect them, the kernel refuses to resolve a relative name through it
# (WinError 1921) and nothing appears outside (native tests).
WRITE_DIRECTORY_SHARE = FILE_SHARE_READ | FILE_SHARE_WRITE
READ_DIRECTORY_SHARE = FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE

# A file opened to delete or rename needs DELETE; the plugin never follows a
# link at the final component.
MUTATE_FILE_ACCESS = DELETE | FILE_READ_ATTRIBUTES | SYNCHRONIZE

ERROR_SHARING_VIOLATION = 32
ERROR_LOCK_VIOLATION = 33

STATUS_SHARING_VIOLATION = 0xC0000043
STATUS_LOCK_NOT_GRANTED = 0xC0000054

_RETRY_SECONDS = 1.0
_EPOCH_DELTA = 11644473600  # seconds from 1601-01-01 to 1970-01-01


# --- structures ------------------------------------------------------------------


class UNICODE_STRING(ctypes.Structure):
    _fields_ = [("Length", wintypes.USHORT), ("MaximumLength", wintypes.USHORT),
                ("Buffer", wintypes.LPWSTR)]


class OBJECT_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Length", wintypes.ULONG), ("RootDirectory", HANDLE),
                ("ObjectName", ctypes.POINTER(UNICODE_STRING)), ("Attributes", wintypes.ULONG),
                ("SecurityDescriptor", ctypes.c_void_p),
                ("SecurityQualityOfService", ctypes.c_void_p)]


class IO_STATUS_BLOCK(ctypes.Structure):
    """The union of ``NTSTATUS Status`` and ``PVOID Pointer``, then ``ULONG_PTR Information``."""

    _fields_ = [("Status", ctypes.c_ssize_t), ("Information", ctypes.c_size_t)]


class _NameInformation(ctypes.Structure):
    """``FILE_RENAME_INFORMATION`` and ``FILE_LINK_INFORMATION`` share this layout."""

    _fields_ = [("ReplaceIfExists", wintypes.BOOLEAN), ("RootDirectory", HANDLE),
                ("FileNameLength", wintypes.ULONG), ("FileName", wintypes.WCHAR * 1)]


class DispositionInformation(ctypes.Structure):
    _fields_ = [("DeleteFile", wintypes.BOOLEAN)]


class FileInfo(ctypes.Structure):
    """``BY_HANDLE_FILE_INFORMATION``."""

    _fields_ = [
        ("attributes", wintypes.DWORD),
        ("creation", wintypes.FILETIME),
        ("access", wintypes.FILETIME),
        ("write", wintypes.FILETIME),
        ("volume", wintypes.DWORD),
        ("size_high", wintypes.DWORD),
        ("size_low", wintypes.DWORD),
        ("links", wintypes.DWORD),
        ("index_high", wintypes.DWORD),
        ("index_low", wintypes.DWORD),
    ]

    @property
    def is_reparse_point(self):
        return bool(self.attributes & FILE_ATTRIBUTE_REPARSE_POINT)

    @property
    def is_directory(self):
        return bool(self.attributes & FILE_ATTRIBUTE_DIRECTORY)

    @property
    def identity(self):
        """``(volume serial, file index)``: the same pair for the same file object, on this host."""
        return (int(self.volume), (int(self.index_high) << 32) | int(self.index_low))


def _declare(dll, name, restype, *argtypes):
    function = getattr(dll, name)
    function.restype = restype
    function.argtypes = argtypes
    return function


_CreateFileW = _declare(_k32, "CreateFileW", HANDLE, wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, HANDLE)
_CloseHandle = _declare(_k32, "CloseHandle", wintypes.BOOL, HANDLE)
_GetFileInformationByHandle = _declare(_k32, "GetFileInformationByHandle", wintypes.BOOL, HANDLE,
                                       ctypes.POINTER(FileInfo))
_GetFinalPathNameByHandleW = _declare(_k32, "GetFinalPathNameByHandleW", wintypes.DWORD, HANDLE,
                                      wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD)
_GetCurrentProcess = _declare(_k32, "GetCurrentProcess", HANDLE)
_DuplicateHandle = _declare(_k32, "DuplicateHandle", wintypes.BOOL, HANDLE, HANDLE, HANDLE,
                            ctypes.POINTER(HANDLE), wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
_SetFileTime = _declare(_k32, "SetFileTime", wintypes.BOOL, HANDLE, ctypes.POINTER(wintypes.FILETIME),
                        ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME))
_FlushFileBuffers = _declare(_k32, "FlushFileBuffers", wintypes.BOOL, HANDLE)

_NtCreateFile = _declare(
    _ntdll, "NtCreateFile", NTSTATUS,
    ctypes.POINTER(HANDLE), wintypes.ULONG, ctypes.POINTER(OBJECT_ATTRIBUTES),
    ctypes.POINTER(IO_STATUS_BLOCK), ctypes.POINTER(ctypes.c_longlong), wintypes.ULONG,
    wintypes.ULONG, wintypes.ULONG, wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG)
_NtSetInformationFile = _declare(
    _ntdll, "NtSetInformationFile", NTSTATUS, HANDLE, ctypes.POINTER(IO_STATUS_BLOCK),
    ctypes.c_void_p, wintypes.ULONG, wintypes.ULONG)
_RtlNtStatusToDosError = _declare(_ntdll, "RtlNtStatusToDosError", wintypes.ULONG, NTSTATUS)


# --- errors and paths ------------------------------------------------------------


def _error(code, filename=None, filename2=None):
    """The ``OSError`` (with its errno subclass) for a Win32 error code."""
    exc = ctypes.WinError(code)
    exc.filename = filename
    exc.filename2 = filename2
    return exc


def _last(filename=None, filename2=None):
    return _error(ctypes.get_last_error(), filename, filename2)


def _nt_error(status, filename=None, filename2=None):
    """The ``OSError`` for an NTSTATUS, through the same mapping Win32 uses."""
    return _error(int(_RtlNtStatusToDosError(status)), filename, filename2)


def extended(path):
    """``path`` as an absolute ``\\\\?\\`` path, which Win32 passes to the file system untouched."""
    text = ntpath.abspath(str(path))
    if text.startswith("\\\\?\\"):
        return text
    if text.startswith("\\\\"):
        return "\\\\?\\UNC\\" + text[2:]
    return "\\\\?\\" + text


def same_path(a, b):
    return ntpath.normcase(a) == ntpath.normcase(b)


def parent_of(path):
    return ntpath.dirname(path)


def child_of(directory_path, name):
    return directory_path.rstrip("\\") + "\\" + name


# --- handles ----------------------------------------------------------------------


def create_file(path, access, share, disposition, flags, name=None):
    """``CreateFileW``; a handle (an int) or ``OSError`` carrying ``name`` as its filename.

    Used only to open the library root by path: everything below it is opened
    relative to the handles this returns.
    """
    handle = _CreateFileW(path, access, share, None, disposition, flags, None)
    if handle is None or handle == INVALID_HANDLE_VALUE:
        raise _last(name if name is not None else path)
    return handle


def open_directory(path, access, share, name=None):
    """Open an existing directory by path without following a reparse point."""
    return create_file(path, access, share, OPEN_EXISTING,
                       FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT, name)


def open_held_directory(path, writable=True):
    """Open a directory to hold it for an operation: ``(handle, write_capable)``.

    A write-capable handle is what a mutation needs (``FILE_ADD_FILE``,
    ``FILE_ADD_SUBDIRECTORY``, ``FILE_DELETE_CHILD``) and it withholds
    ``FILE_SHARE_DELETE`` so the held directory cannot be renamed or deleted.
    Write sharing is granted (hard link and rename need it), so another process
    can set a reparse point on an empty held directory; child operations are
    object-relative to the handle, and the kernel refuses to resolve through
    one (WinError 1921), so that cannot redirect a write. A directory the caller may read but not write
    falls back to a read-only handle, which cannot create children at all.
    """
    if writable:
        try:
            return open_directory(path, WRITE_DIRECTORY_ACCESS, WRITE_DIRECTORY_SHARE), True
        except PermissionError:
            pass
    return open_directory(path, READ_DIRECTORY_ACCESS, READ_DIRECTORY_SHARE), False


def close(handle):
    """Close a handle. Never raises."""
    if handle:
        _CloseHandle(handle)


def duplicate(handle):
    """An independently closable handle on the same open directory."""
    duplicated = HANDLE()
    process = _GetCurrentProcess()
    if not _DuplicateHandle(process, handle, process, ctypes.byref(duplicated), 0, False, 2):  # DUPLICATE_SAME_ACCESS
        raise _last()
    return duplicated.value


def file_info(handle):
    info = FileInfo()
    if not _GetFileInformationByHandle(handle, ctypes.byref(info)):
        raise _last()
    return info


def identity(handle):
    """``(st_dev, st_ino)`` exactly as ``os.stat`` reports them for the object ``handle`` is open on."""
    import os

    fd = to_fd(duplicate(handle), 0)
    try:
        stat_result = os.fstat(fd)
    finally:
        os.close(fd)
    return (stat_result.st_dev, stat_result.st_ino)


def final_path(handle):
    """The handle's own ``\\\\?\\`` path name, as the file system reports it now.

    Used for reporting and as a containment cross-check, never to build a path
    for a write.
    """
    size = 512
    while True:
        buffer = ctypes.create_unicode_buffer(size)
        length = _GetFinalPathNameByHandleW(handle, buffer, size, 0)  # FILE_NAME_NORMALIZED | VOLUME_NAME_DOS
        if length == 0:
            raise _last()
        if length < size:
            return buffer.value
        size = length + 1


def to_fd(handle, flags):
    """A binary CRT descriptor owning ``handle``; the handle is closed if that fails."""
    try:
        return msvcrt.open_osfhandle(handle, flags)
    except BaseException:
        close(handle)
        raise


def set_mtime(handle, seconds):
    """Set a file's access and write times; ``handle`` needs ``FILE_WRITE_ATTRIBUTES``."""
    ticks = int((seconds + _EPOCH_DELTA) * 10_000_000)
    stamp = wintypes.FILETIME(ticks & 0xFFFFFFFF, ticks >> 32)
    if not _SetFileTime(handle, None, ctypes.byref(stamp), ctypes.byref(stamp)):
        raise _last()


def flush(handle):
    """Best-effort flush of a file or directory handle. Returns False instead of raising."""
    return bool(_FlushFileBuffers(handle))


# --- object-relative opens ----------------------------------------------------------


def _object_name(name):
    """``(UNICODE_STRING, keepalive)`` for one name component; no copy is made of the text."""
    buffer = ctypes.create_unicode_buffer(name)
    length = len(name.encode("utf-16-le"))
    return UNICODE_STRING(length, length + 2, ctypes.cast(buffer, wintypes.LPWSTR)), buffer


def _open_relative(parent, name, access, share, disposition, options, filename=None):
    """``NtCreateFile`` for one child name of the directory object ``parent`` holds.

    ``FILE_SYNCHRONOUS_IO_NONALERT`` is what ``CreateFileW`` adds on a handle that
    is not overlapped, and what makes the handle usable by the CRT (``ReadFile``
    without an overlapped structure) and by ``NtSetInformationFile``. It requires
    ``SYNCHRONIZE``, and every child open is followed by an attribute check, so
    ``SYNCHRONIZE`` and ``FILE_READ_ATTRIBUTES`` are added to every access mask
    here rather than at each call site.
    """
    access |= SYNCHRONIZE | FILE_READ_ATTRIBUTES
    object_name, keepalive = _object_name(name)  # noqa: F841 - the buffer must outlive the call
    attributes = OBJECT_ATTRIBUTES(ctypes.sizeof(OBJECT_ATTRIBUTES), parent,
                                   ctypes.pointer(object_name), OBJ_CASE_INSENSITIVE, None, None)
    status = IO_STATUS_BLOCK()
    handle = HANDLE()
    result = _NtCreateFile(ctypes.byref(handle), access, ctypes.byref(attributes), ctypes.byref(status),
                           None, 0, share, disposition, options | FILE_SYNCHRONOUS_IO_NONALERT, None, 0)
    if result < 0:
        raise _nt_error(result, filename if filename is not None else name)
    return handle.value


def open_child_directory(parent, name, *, writable=True, create=False, open_if=False, filename=None):
    """Open or create the child directory ``name`` of ``parent``, object-relative.

    ``create`` is ``FILE_CREATE`` and creates the directory outright
    (``FILE_DIRECTORY_FILE``), returning the held handle with no separate open
    afterwards. Otherwise the entry must already exist (``open_if`` allows
    either) and is opened with ``FILE_OPEN_REPARSE_POINT``; the caller checks
    the type and refuses a reparse point.
    """
    access, share = ((WRITE_DIRECTORY_ACCESS, WRITE_DIRECTORY_SHARE) if writable
                     else (READ_DIRECTORY_ACCESS, READ_DIRECTORY_SHARE))
    if create:
        return _open_relative(parent, name, access, share, FILE_CREATE,
                              FILE_DIRECTORY_FILE | FILE_OPEN_REPARSE_POINT, filename)
    disposition = FILE_OPEN_IF if open_if else FILE_OPEN
    return _open_relative(parent, name, access, share, disposition, FILE_OPEN_REPARSE_POINT, filename)


def open_child_file(parent, name, access, share, *, create=False, open_if=False, filename=None):
    """Open or create the child file ``name`` of ``parent``, object-relative and never following a link."""
    if create:
        return _open_relative(parent, name, access, share, FILE_CREATE, FILE_OPEN_REPARSE_POINT, filename)
    disposition = FILE_OPEN_IF if open_if else FILE_OPEN
    return _open_relative(parent, name, access, share, disposition, FILE_OPEN_REPARSE_POINT, filename)


# --- object-relative mutations -------------------------------------------------------


def open_child_for_delete(parent, name, filename=None):
    """Open a child entry just to remove it: ``DELETE`` access, object-relative, never following a link."""
    return _open_relative(parent, name, DELETE | FILE_READ_ATTRIBUTES,
                          FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                          FILE_OPEN, FILE_OPEN_REPARSE_POINT, filename)


def _retrying_nt(call, filename=None, filename2=None):
    """Run an NtSetInformationFile call, retrying briefly while a scanner or indexer holds the file."""
    deadline = time.monotonic() + _RETRY_SECONDS
    while True:
        status = call()
        if status >= 0:
            return
        code = status & 0xFFFFFFFF
        if code in (STATUS_SHARING_VIOLATION, STATUS_LOCK_NOT_GRANTED) and time.monotonic() < deadline:
            time.sleep(0.01)
            continue
        raise _nt_error(status, filename, filename2)


def _named_information(name, parent, replace):
    """``(buffer, information, size)`` for a ``FILE_RENAME_INFORMATION``/link buffer."""
    encoded = name.encode("utf-16-le")
    header = _NameInformation.FileName.offset
    size = header + len(encoded)
    buffer = ctypes.create_string_buffer(size)
    information = ctypes.cast(buffer, ctypes.POINTER(_NameInformation)).contents
    information.ReplaceIfExists = 1 if replace else 0
    information.RootDirectory = parent
    information.FileNameLength = len(encoded)
    ctypes.memmove(ctypes.addressof(buffer) + header, encoded, len(encoded))
    return buffer, information, size


def move(handle, target, *, parent, replace, source_name=None, target_name=None):
    """Rename the open ``handle`` to ``target`` under ``parent``, object-relative.

    ``handle`` must be a handle to the source entry, opened with ``DELETE``
    access and without following a link. The rename never follows a link at the
    new name: with ``replace`` an existing entry there is replaced as itself,
    which is what ``os.replace`` does.
    """
    buffer, information, size = _named_information(target, parent, replace)
    status = IO_STATUS_BLOCK()
    keepalive = (buffer, information)  # noqa: F841 - both must outlive the call
    _retrying_nt(lambda: _NtSetInformationFile(handle, ctypes.byref(status), ctypes.byref(information), size,
                                               FILE_RENAME_INFORMATION),
                 source_name or "source", target_name or target)


def hard_link(target, handle, *, parent, target_name=None, source_name=None):
    """Make ``target`` a new name for the open ``handle``, object-relative. Fails if it exists.

    ``handle`` must be a handle to the existing entry, opened without following a
    link; ``parent`` is the held directory the new name belongs to. The two names
    stay first so tests can substitute this call through ``patch_link``.
    """
    buffer, information, size = _named_information(target, parent, False)
    status = IO_STATUS_BLOCK()
    keepalive = (buffer, information)  # noqa: F841 - both must outlive the call
    _retrying_nt(lambda: _NtSetInformationFile(handle, ctypes.byref(status), ctypes.byref(information), size,
                                               FILE_LINK_INFORMATION),
                 source_name or "source", target_name or target)


def delete_on_close(handle, filename=None):
    """Mark the open entry for deletion when ``handle`` closes (``FileDispositionInformation``)."""
    information = DispositionInformation(1)
    status = IO_STATUS_BLOCK()
    _retrying_nt(lambda: _NtSetInformationFile(handle, ctypes.byref(status), ctypes.byref(information),
                                               ctypes.sizeof(information), FILE_DISPOSITION_INFORMATION),
                 filename)
