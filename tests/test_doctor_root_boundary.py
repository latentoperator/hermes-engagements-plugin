"""Doctor keeps one root boundary and never creates a file or a lock.

Doctor used to classify the metadata on one open, then call
``open_library`` and read the root again. Between those observations a root
could be replaced (doctor then described, or read, a directory the caller never
named), a symlink could send the read outside the library, and a metadata file
that turned invalid made the opener create ``.engagements/locks/_library.lock``
even though doctor must never write.

Each test swaps the root, or the metadata, inside the one seam between the
metadata verdict and the discovery walk, which is exactly the boundary the
report reproduces. Fictional data only.
"""

import os
import shutil
import sys

import pytest
from conftest import record_bytes, write
from engagements import doctor as doctor_module
from engagements.doctor import run_doctor

LIBRARY_YAML = "format: 1\nlibrary_id: 33333333-3333-4333-8333-333333333333\n"
ROOT_CHANGED = {"path": ".", "reason": "root_changed"}


def doctor(root, **kwargs):
    kwargs.setdefault("home", "/home/tester")
    kwargs.setdefault("os_name", "posix")
    kwargs.setdefault("exists", lambda path: False)
    kwargs.setdefault("realpath", lambda path: path)
    return run_doctor(root, **kwargs)


def findings(report, code):
    return [finding for finding in report.findings if finding.code == code]


def make_library(root, *, client_id="acme-bakery", name="Acme Bakery", metadata=True):
    root.mkdir(parents=True, exist_ok=True)
    if metadata:
        write(root, ".engagements/library.yaml", LIBRARY_YAML)
    write(root, f"{name}/Client.md", record_bytes("client", client_id, name))
    return root


def replace_directory(root, source):
    """Put a *different* directory at ``root``'s path, leaving the old one on disk.

    The old inode moves aside, so a held descriptor keeps addressing it and any
    later open of ``root`` sees the replacement -- exactly the swap the root
    identity exists to catch.
    """
    moved = root.with_name(root.name + "-replaced")
    if moved.exists():
        shutil.rmtree(moved)
    os.rename(root, moved)
    shutil.copytree(source, root)


@pytest.fixture
def swap_after_metadata(monkeypatch):
    """Run a callback once, after the metadata verdict and before the walk."""
    original = doctor_module._metadata_state

    def install(callback):
        def racing(top):
            state = original(top)
            callback()
            return state

        monkeypatch.setattr(doctor_module, "_metadata_state", racing)

    return install


# --- missing metadata ---------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="Windows holds the root handle without delete sharing, so the root cannot be swapped; see test_windows_native.py")
def test_a_root_replaced_after_missing_metadata_is_not_read(tmp_path, swap_after_metadata):
    root = make_library(tmp_path / "library", metadata=False)
    replacement = make_library(tmp_path / "replacement", client_id="replaced-client", name="Replaced Client")
    swap_after_metadata(lambda: replace_directory(root, replacement))

    report = doctor(root)

    assert report.checked == 0
    (finding,) = findings(report, "io_error")
    assert finding.message == "The library could not be read."
    assert not any(f.path and f.path.startswith("Replaced Client") for f in report.findings)
    assert findings(report, "library_yaml")


# --- invalid metadata ---------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="Windows holds the root handle without delete sharing, so the root cannot be swapped; see test_windows_native.py")
def test_a_root_replaced_after_invalid_metadata_is_not_read(tmp_path, swap_after_metadata):
    root = make_library(tmp_path / "library")
    write(root, ".engagements/library.yaml", "format: 2\nlibrary_id: nope\n")
    replacement = make_library(tmp_path / "replacement", client_id="replaced-client", name="Replaced Client")
    swap_after_metadata(lambda: replace_directory(root, replacement))

    report = doctor(root)

    assert report.checked == 0
    assert findings(report, "io_error")
    assert findings(report, "library_yaml")
    assert not any(f.path and f.path.startswith("Replaced Client") for f in report.findings)


# --- valid metadata -----------------------------------------------------------


def test_a_root_swapped_for_a_symlink_is_refused(tmp_path, swap_after_metadata):
    root = make_library(tmp_path / "library")
    outside = make_library(tmp_path / "outside", client_id="outside-client", name="Outside Client")
    moved = tmp_path / "library-moved"

    def swap():
        os.rename(root, moved)
        os.symlink(outside, root)

    swap_after_metadata(swap)
    report = doctor(root)

    assert report.checked == 0
    (finding,) = findings(report, "io_error")
    assert finding.message == "The library could not be read."
    assert "Outside Client" not in report.render_text()
    assert findings(report, "library_yaml") == []


def test_a_root_replaced_after_valid_metadata_is_refused(tmp_path, swap_after_metadata):
    root = make_library(tmp_path / "library")
    replacement = make_library(tmp_path / "replacement", client_id="replaced-client", name="Replaced Client")
    swap_after_metadata(lambda: replace_directory(root, replacement))

    report = doctor(root)

    assert report.checked == 0
    assert findings(report, "io_error")
    assert not any(f.path and f.path.startswith("Replaced Client") for f in report.findings)


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks are not available")
def test_invalid_metadata_after_its_verdict_creates_no_lock(tmp_path, swap_after_metadata):
    """The opener used to take the library lock on an invalid metadata file."""
    root = make_library(tmp_path / "library")
    (root / ".engagements" / "locks").mkdir()

    swap_after_metadata(lambda: write(root, ".engagements/library.yaml", "format: 2\nlibrary_id: nope\n"))
    report = doctor(root)

    assert list((root / ".engagements" / "locks").iterdir()) == []
    # The verdict was taken before the change and describes the verified bytes.
    assert findings(report, "library_yaml") == []
    assert report.checked == 1


def test_doctor_never_creates_a_lock_directory(tmp_path):
    root = make_library(tmp_path / "library")
    write(root, ".engagements/library.yaml", "format: 2\nlibrary_id: nope\n")
    before = sorted((p.relative_to(root).as_posix(), p.is_dir()) for p in root.rglob("*"))

    report = doctor(root)

    after = sorted((p.relative_to(root).as_posix(), p.is_dir()) for p in root.rglob("*"))
    assert before == after
    assert not (root / ".engagements" / "locks").exists()
    assert findings(report, "library_yaml")
