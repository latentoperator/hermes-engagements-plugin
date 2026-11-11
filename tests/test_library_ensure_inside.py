"""``ensure_inside`` maps canonicalization failures to ``io_error`` and still checks paths for
an opened library. Fictional data only.

The public ``ensure_inside`` used to map only ``ValueError`` around root and
path canonicalization, so a relative root resolved from a deleted current
directory leaked a raw ``FileNotFoundError`` instead of the documented
``io_error`` (the same defect ``open_library`` had).
"""

import errno
import os

import pytest
from engagements.errors import EngagementsError
from engagements.library import ensure_inside, open_library

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX: removing the current directory")


@pytest.fixture
def deleted_cwd(tmp_path):
    saved = os.open(".", os.O_RDONLY)
    gone = tmp_path / "gone"
    gone.mkdir()
    try:
        os.chdir(gone)
        os.rmdir(gone)
        yield
    finally:
        os.fchdir(saved)
        os.close(saved)


@pytest.mark.parametrize(("root", "path"), [(".", "Client.md"), ("Acme Bakery", "Client.md"), ("", "Notes.md")])
def test_canonicalization_failure_is_io_error(deleted_cwd, root, path):
    with pytest.raises(EngagementsError) as exc:
        ensure_inside(root, path)
    assert exc.value.code == "io_error"
    assert exc.value.details["path"] == path
    assert exc.value.details.get("errno") == errno.errorcode[errno.ENOENT]


def test_a_bytes_root_with_a_text_path_is_invalid_op(tmp_path):
    with pytest.raises(EngagementsError) as exc:
        ensure_inside(os.fsencode(tmp_path), "Client.md")
    assert (exc.value.code, exc.value.details) == ("invalid_op", {"field": "path", "reason": "invalid_value"})


def test_absolute_inputs_still_work_from_a_deleted_cwd(tmp_path, deleted_cwd):
    root = tmp_path / "library"
    root.mkdir()
    assert ensure_inside(root, "Acme Bakery/Client.md") == root / "Acme Bakery" / "Client.md"
    with pytest.raises(EngagementsError) as exc:
        ensure_inside(root, "../elsewhere.md")
    assert exc.value.code == "outside_root"


def test_an_opened_library_still_checks_paths(tmp_path):
    lib = open_library(tmp_path, create_default=True)
    assert ensure_inside(lib, "Acme Bakery/Client.md") == tmp_path.resolve() / "Acme Bakery" / "Client.md"
    with pytest.raises(EngagementsError) as exc:
        ensure_inside(lib, "/elsewhere/Riverbend Dental.md")
    assert exc.value.code == "outside_root"
