"""Every fixture library ships the metadata a fresh checkout needs.

``.engagements/`` is ignored (spec 4.5 keeps it out of a vault's
file tree), which also hid ``tests/fixtures/studio/.engagements/library.yaml``.
A pristine checkout then opened the studio fixture with ``library_missing`` and
150 tests failed, while a developer worktree that still had the untracked file
passed. These checks fail on a fresh checkout of the broken head and pass once
the narrow ``.gitignore`` exceptions are committed.

Fictional data only.
"""

import shutil
import subprocess
import uuid
from pathlib import Path

import pytest
import yaml

FIXTURES = Path(__file__).resolve().parent / "fixtures"
METADATA = Path(".engagements") / "library.yaml"
USED_AS_A_LIBRARY = ("library", "studio")


def metadata_document(name):
    path = FIXTURES / name / METADATA
    assert path.is_file(), f"fixture {name!r} has no committed {METADATA.as_posix()}"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", USED_AS_A_LIBRARY)
def test_fixture_libraries_have_valid_metadata(name):
    document = metadata_document(name)
    assert isinstance(document, dict)
    assert document["format"] == 1
    assert isinstance(document["library_id"], str)
    uuid.UUID(document["library_id"])


def test_every_fixture_directory_used_as_a_library_is_covered():
    """A new fixture library must be added to this file's list, not silently pass."""
    candidates = {path.parent.parent.name for path in FIXTURES.glob("*/.engagements/library.yaml")}
    assert candidates <= set(USED_AS_A_LIBRARY), (
        f"fixture libraries missing a metadata check: {sorted(candidates - set(USED_AS_A_LIBRARY))}")


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_fixture_metadata_is_tracked_by_git():
    """A file that exists on disk but is ignored does not survive a fresh clone."""
    repo = FIXTURES.parent.parent
    if not (repo / ".git").exists():
        pytest.skip("not a git checkout")
    for name in USED_AS_A_LIBRARY:
        relative = (Path("tests") / "fixtures" / name / METADATA).as_posix()
        result = subprocess.run(["git", "-C", str(repo), "ls-files", "--error-unmatch", relative],
                                capture_output=True, text=True)
        assert result.returncode == 0, f"{relative} is not tracked: {result.stderr.strip()}"
