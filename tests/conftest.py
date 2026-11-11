"""Shared helpers for the library tests. Fictional data only."""

import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from writer_support import make_library

FIXTURE_LIBRARY = Path(__file__).parent / "fixtures" / "library"
TZ = timezone(timedelta(hours=-6))


def _can_symlink():
    """True when this host can create symlinks (Windows needs a privilege or developer mode)."""
    import tempfile

    with tempfile.TemporaryDirectory() as scratch:
        try:
            os.symlink(Path(scratch) / "target", Path(scratch) / "link")
        except (OSError, NotImplementedError):
            return False
    return True


CAN_SYMLINK = _can_symlink()


def patch_link(monkeypatch, replacement):
    """Replace the platform's hard-link call; ``replacement(source, target, **kw)`` is the os.link shape."""
    if sys.platform == "win32":
        from engagements import _winfs

        monkeypatch.setattr(_winfs, "hard_link", lambda target, source, *a, **kw: replacement(source, target))
    else:
        monkeypatch.setattr(os, "link", replacement)


def patch_replace(monkeypatch, replacement):
    """Replace the platform's atomic rename-over call; ``replacement`` takes anything and may raise."""
    if sys.platform == "win32":
        from engagements import _winfs

        monkeypatch.setattr(_winfs, "move", replacement)
    else:
        from engagements import writer

        monkeypatch.setattr(writer.os, "replace", replacement)


def clock():
    return datetime(2026, 11, 7, 14, 2, tzinfo=TZ)


@pytest.fixture
def library_root(tmp_path):
    """A writable copy of the fixture library."""
    root = tmp_path / "library"
    shutil.copytree(FIXTURE_LIBRARY, root)
    return root


@pytest.fixture
def lib(tmp_path):
    """The writer-shaped library (writer, binding tests). The library_* test modules
    override this with an opened fixture library; see `opened_lib` below."""
    return make_library(tmp_path)


@pytest.fixture
def opened_lib(library_root):
    """The fixture library opened with open_library; library_* modules alias it as `lib`."""
    from engagements.library import open_library

    return open_library(library_root, create_default=False)


def record_bytes(kind, id_, name, *, client=None, aliases=None, status="active", extra=""):
    """A small valid record, written by hand so tests do not depend on the renderer."""
    lines = ["---", f"engagements: {kind}", f"id: {id_}"]
    if client:
        lines.append(f"client: {client}")
    lines.append(f"name: {name}")
    if aliases is not None:
        lines.append("aliases: [" + ", ".join(aliases) + "]")
    lines.append(f"status: {status}")
    lines.append("updated: 2026-11-05")
    lines.append("---")
    lines.append("")
    lines.append("## Summary")
    lines.append(extra)
    lines.append("")
    return "\n".join(lines).encode()


def write(root, rel, data):
    path = Path(root) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode())
    return path


@pytest.fixture
def monkeypatch(monkeypatch):
    """pytest's monkeypatch, plus USERPROFILE following HOME on Windows.

    ``os.path.expanduser('~')`` reads HOME on POSIX and USERPROFILE on Windows, so a test that
    points HOME at a temporary home must move both for the plugin to see that home.
    """
    if sys.platform == "win32":
        set_env = monkeypatch.setenv

        def setenv(name, value, prepend=None):
            set_env(name, value, prepend)
            if name == "HOME":
                set_env("USERPROFILE", value, prepend)

        monkeypatch.setenv = setenv
    return monkeypatch
