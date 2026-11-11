"""Open, lock, publication and scan boundaries: symlinked locks and directories, the 64 KiB
discovery window, duplicated ids, a defective status and first use on the exclusive-create
fallback. Fictional data only."""

import errno
import os
import sys
import threading
import time

import pytest
from conftest import clock, patch_link, record_bytes, write
from engagements import library as library_module
from engagements.errors import EngagementsError
from engagements.library import open_library


@pytest.fixture
def lib(opened_lib):
    return opened_lib

WINDOW = 65536


def make(lib, kind="client", name="Foo Co", **kwargs):
    kwargs.setdefault("profile", "studio-dev")
    kwargs.setdefault("today", clock)
    return lib.create(kind, name, **kwargs)


def fails(lib, code, **kwargs):
    with pytest.raises(EngagementsError) as e:
        make(lib, **kwargs)
    assert e.value.code == code, (e.value.code, e.value.details)
    return e.value


def everything(path):
    return sorted(str(p.relative_to(path)) for p in path.rglob("*"))


@pytest.fixture
def outside(tmp_path):
    path = tmp_path / "outside"
    path.mkdir()
    return path


# --- the library lock file is never followed -------------------------------


def test_dangling_lock_symlink_is_refused_and_creates_nothing_outside(lib, library_root, outside):
    locks = library_root / ".engagements" / "locks"
    locks.mkdir(parents=True, exist_ok=True)
    (locks / "_library.lock").symlink_to(outside / "planted.lock")
    err = fails(lib, "io_error", name="Foo Co")
    assert err.details["reason"] == "symlink"
    assert not os.path.isabs(err.details["path"])
    assert everything(outside) == []
    assert not (library_root / "Foo Co").exists()


def test_lock_symlink_to_an_existing_outside_file_is_refused(lib, library_root, outside):
    target = outside / "existing.lock"
    target.write_bytes(b"keep")
    locks = library_root / ".engagements" / "locks"
    locks.mkdir(parents=True, exist_ok=True)
    (locks / "_library.lock").symlink_to(target)
    fails(lib, "io_error", name="Foo Co")
    assert target.read_bytes() == b"keep"
    assert not (library_root / "Foo Co").exists()


@pytest.mark.parametrize("kind", ["directory", "fifo"])
def test_lock_path_that_is_not_a_regular_file_is_refused(lib, library_root, kind):
    locks = library_root / ".engagements" / "locks"
    locks.mkdir(parents=True, exist_ok=True)
    lock = locks / "_library.lock"
    if kind == "directory":
        lock.mkdir()
    else:
        if not hasattr(os, "mkfifo"):
            pytest.skip("no FIFOs on this platform")
        os.mkfifo(lock)
    err = fails(lib, "io_error", name="Foo Co")
    assert err.details.get("reason") in {"not_a_regular_file", None}
    assert not (library_root / "Foo Co").exists()


def test_symlinked_metadata_directory_is_refused_by_create(lib, library_root, outside):
    meta = library_root / ".engagements"
    for child in sorted(meta.rglob("*"), reverse=True):
        child.rmdir() if child.is_dir() else child.unlink()
    meta.rmdir()
    meta.symlink_to(outside)
    fails(lib, "io_error", name="Foo Co")
    assert everything(outside) == []


# --- publication never follows a swapped directory ------------------------


def _swap_after_mkdir(monkeypatch, root, name, outside):
    """Replace the directory ``name`` with a link to ``outside`` right after it is created."""
    real_mkdir = os.mkdir
    swapped = []

    def mkdir(path, mode=0o777, *, dir_fd=None):
        if dir_fd is None:
            real_mkdir(path, mode)
        else:
            real_mkdir(path, mode, dir_fd=dir_fd)
        if not swapped and os.path.basename(os.fspath(path)) == name:
            victim = root / name
            os.rmdir(victim)
            os.symlink(outside, victim)
            swapped.append(victim)

    monkeypatch.setattr(os, "mkdir", mkdir)
    return swapped


@pytest.mark.skipif(sys.platform == "win32", reason="simulates a POSIX directory swap with os.mkdir/os.symlink hooks; the Windows threat tests are in test_windows_native.py")
def test_client_directory_swapped_for_a_link_after_mkdir_is_refused(lib, library_root, outside, monkeypatch):
    swapped = _swap_after_mkdir(monkeypatch, library_root, "Foo Co", outside)
    fails(lib, "io_error", name="Foo Co")
    assert swapped
    assert everything(outside) == []
    monkeypatch.undo()
    assert "foo-co" not in {e.id for e in lib.discover().entries}


@pytest.mark.skipif(sys.platform == "win32", reason="simulates a POSIX directory swap with os.mkdir/os.symlink hooks; the Windows threat tests are in test_windows_native.py")
def test_projects_directory_swapped_for_a_link_after_mkdir_is_refused(lib, library_root, outside, monkeypatch):
    make(lib, "client", "Foo Co")
    swapped = _swap_after_mkdir(monkeypatch, library_root / "Foo Co", "Projects", outside)
    fails(lib, "io_error", kind="project", name="Menu Refresh", client="foo-co")
    assert swapped
    assert everything(outside) == []


@pytest.mark.skipif(sys.platform == "win32", reason="simulates a POSIX directory swap with os.mkdir/os.symlink hooks; the Windows threat tests are in test_windows_native.py")
def test_client_directory_replaced_after_validation_cannot_redirect_the_project(lib, library_root, outside,
                                                                                monkeypatch):
    real = library_module._listdir
    swapped = []

    def listdir(directory):
        name = getattr(directory, "relpath", None) or os.fspath(directory)
        if not swapped and os.path.basename(name) == "Acme Bakery":
            os.rename(library_root / "Acme Bakery", library_root / "Moved Acme")
            os.symlink(outside, library_root / "Acme Bakery")
            swapped.append(True)
        return real(directory)

    monkeypatch.setattr(library_module, "_listdir", listdir)
    try:
        make(lib, "project", "Menu Refresh", client="acme-bakery")
    except EngagementsError as exc:
        # Refusing is fine; following the link is not. The held descriptor would
        # otherwise publish into the real, moved directory, which is still inside.
        assert exc.code in {"io_error", "outside_root"}
    assert swapped
    assert everything(outside) == []


# --- the 64 KiB window only closes on a complete line ------------------------


def _spy_reads(monkeypatch):
    reads = []
    real_read = os.read

    def spy(fd, n):
        data = real_read(fd, n)
        reads.append((os.fstat(fd).st_size, len(data)))
        return data

    monkeypatch.setattr("engagements.library.os.read", spy)
    return reads


def _window_ending_in_delimiter(identifier):
    head = f"---\nengagements: client\nid: {identifier}\nname: Window Trick\nnote: ".encode()
    fill = WINDOW - len(head) - len(b"\n---")
    data = head + b"a" * fill + b"\n---"
    assert len(data) == WINDOW
    return data


def test_delimiter_continuing_past_the_window_is_not_read_further(lib, library_root, monkeypatch):
    data = _window_ending_in_delimiter("window-trick") + b"NOT-A-CLOSING-LINE\n" + b"secret padding\n" * 6000
    assert WINDOW < len(data) < 1024 * 1024
    write(library_root, "Notes/trick.md", data)
    before = len(lib.discover().entries)
    reads = _spy_reads(monkeypatch)
    d = lib.discover()
    assert sum(n for size, n in reads if size == len(data)) <= WINDOW
    assert "window-trick" not in {e.id for e in d.entries}
    assert all(e.relpath != "Notes/trick.md" for e in d.entries)
    assert len(d.entries) == before


def test_closing_line_whose_newline_falls_just_outside_the_window_is_not_read_further(lib, library_root,
                                                                                     monkeypatch):
    data = _window_ending_in_delimiter("newline-outside") + b"\n\n## Summary\n" + b"filler\n" * 12000
    write(library_root, "Notes/edge.md", data)
    reads = _spy_reads(monkeypatch)
    d = lib.discover()
    assert sum(n for size, n in reads if size == len(data)) <= WINDOW
    assert all(e.relpath != "Notes/edge.md" for e in d.entries)


def test_record_whose_closing_line_ends_exactly_at_the_window_is_found(lib, library_root):
    head = b"---\nengagements: client\nid: exact-edge\nname: Exact Edge\nnote: "
    fill = WINDOW - len(head) - len(b"\n---\n")
    data = head + b"a" * fill + b"\n---\n"
    assert len(data) == WINDOW
    write(library_root, "Exact/Client.md", data + b"\n## Summary\n" + b"body\n" * 20000)
    entry = next(e for e in lib.discover().entries if e.id == "exact-edge")
    assert entry.kind == "client" and entry.name == "Exact Edge"


def test_file_that_ends_on_the_delimiter_at_the_window_is_a_record(lib, library_root):
    write(library_root, "Ends/Client.md", _window_ending_in_delimiter("ends-at-window"))
    assert "ends-at-window" in {e.id for e in lib.discover().entries}


# --- duplicated ids never select a record ------------------------------------


def test_kind_filter_does_not_hide_a_duplicated_id(lib, library_root):
    write(library_root, "Acme Bakery/Projects/Same.md",
          record_bytes("project", "acme-bakery", "Same Id Project", client="acme-bakery"))
    for kind in (None, "client", "project"):
        with pytest.raises(EngagementsError) as e:
            lib.resolve("acme-bakery", kind=kind)
        assert e.value.code == "ambiguous"
        assert sorted(c["path"] for c in e.value.details["candidates"]) == [
            "Acme Bakery/Client.md", "Acme Bakery/Projects/Same.md"]


def test_null_kind_record_sharing_an_id_makes_it_ambiguous(lib, library_root):
    write(library_root, "Conflict/Client.md",
          b"---\nengagements: client\nid: acme-bakery\nname: C\nengagements: project\n---\n")
    with pytest.raises(EngagementsError) as e:
        lib.resolve("acme-bakery")
    assert e.value.code == "ambiguous"
    assert sorted(c["path"] for c in e.value.details["candidates"]) == ["Acme Bakery/Client.md", "Conflict/Client.md"]


@pytest.mark.parametrize("ref", ["Acme Copy", "copycat"])
def test_unique_name_or_alias_of_a_duplicated_id_returns_every_duplicate(lib, library_root, ref):
    write(library_root, "Copy/Client.md", record_bytes("client", "acme-bakery", "Acme Copy", aliases=["Copycat"]))
    with pytest.raises(EngagementsError) as e:
        lib.resolve(ref)
    assert e.value.code == "ambiguous"
    assert sorted(c["path"] for c in e.value.details["candidates"]) == ["Acme Bakery/Client.md", "Copy/Client.md"]


def test_project_name_within_a_client_with_a_duplicated_id_is_ambiguous(lib, library_root):
    write(library_root, "Elsewhere/Client.md", record_bytes("client", "acme-website-2026", "Elsewhere"))
    with pytest.raises(EngagementsError) as e:
        lib.resolve("Website 2026", kind="project", client="acme-bakery")
    assert e.value.code == "ambiguous"
    assert sorted(c["path"] for c in e.value.details["candidates"]) == [
        "Acme Bakery/Projects/Website 2026.md", "Elsewhere/Client.md"]


def test_unique_ids_and_names_still_resolve(lib):
    assert lib.resolve("acme-bakery").relpath == "Acme Bakery/Client.md"
    assert lib.resolve("Website 2026", kind="project").id == "acme-website-2026"


# --- a defective status never hides a record -----------------------------------


@pytest.mark.parametrize("status_lines", [
    b"status: |\n  active\n",
    b"status: |\n  paused\n",
    b"status: active\nstatus: active\n",
])
def test_defective_status_is_unreadable_and_visible_by_default(lib, library_root, status_lines):
    data = (b"---\nengagements: client\nid: odd-status\nname: Odd Status\n" + status_lines
            + b"updated: 2026-11-05\n---\n\n## Summary\n")
    write(library_root, "Odd/Client.md", data)
    rows = [r for r in lib.find("").results if r["id"] == "odd-status"]
    assert len(rows) == 1
    assert rows[0]["valid"] is False and rows[0]["status"] is None


def test_recognized_inactive_statuses_are_still_hidden(lib, library_root):
    write(library_root, "Old/Client.md", record_bytes("client", "old-co", "Old Co", status="former"))
    write(library_root, "Old/Projects/Done.md", record_bytes("project", "old-done", "Done", client="old-co",
                                                             status="historical"))
    write(library_root, "Paused/Client.md", record_bytes("client", "paused-co", "Paused Co", status="paused"))
    shown = {r["id"] for r in lib.find("").results}
    assert not {"old-co", "old-done", "paused-co"} & shown
    assert {"old-co", "old-done", "paused-co"} <= {r["id"] for r in lib.find("", include_inactive=True).results}


# --- first use on the exclusive-create fallback --------------------------------


@pytest.mark.parametrize("second_create_default", [True, False])
def test_fallback_first_use_never_exposes_a_partial_library_yaml(tmp_path, monkeypatch, second_create_default):
    def unsupported(src, dst, **kw):
        raise OSError(errno.EOPNOTSUPP, "links unsupported")

    patch_link(monkeypatch, unsupported)
    root = tmp_path / "library"
    paused = threading.Event()
    release = threading.Event()
    real_write_all = library_module._write_all
    seen = []

    def slow_write_all(fd, data):
        if data.startswith(b"format: 1"):
            seen.append(data)
            if len(seen) == 2:  # the exclusive final file, after the temp write and failed link
                paused.set()
                assert release.wait(10)
        return real_write_all(fd, data)

    monkeypatch.setattr(library_module, "_write_all", slow_write_all)
    results = {}

    def first():
        results["first"] = open_library(root, create_default=True)

    def second():
        try:
            results["second"] = open_library(root, create_default=second_create_default)
        except EngagementsError as exc:
            results["second"] = exc

    a = threading.Thread(target=first)
    a.start()
    assert paused.wait(10)
    assert (root / ".engagements" / "library.yaml").read_bytes() == b""  # the prefix window that used to stay empty
    b = threading.Thread(target=second)
    b.start()
    time.sleep(0.3)
    assert "second" not in results, results.get("second")  # waits for the initializer instead of failing
    release.set()
    a.join(10)
    b.join(10)
    assert results["first"].created is True
    assert not isinstance(results["second"], EngagementsError), results["second"].details
    assert results["second"].library_id == results["first"].library_id
    assert results["second"].created is False


def test_genuinely_invalid_library_yaml_is_still_refused_and_kept(library_root):
    path = library_root / ".engagements" / "library.yaml"
    (library_root / ".engagements" / "locks").mkdir(exist_ok=True)
    path.write_bytes(b"")
    for create_default in (False, True):
        with pytest.raises(EngagementsError) as e:
            open_library(library_root, create_default=create_default)
        assert e.value.code == "library_missing"
    assert path.read_bytes() == b""


# --- a client never reports a client relationship ------------------------------


def test_client_with_an_extra_client_property_reports_null_client(lib, library_root):
    data = record_bytes("client", "extra-co", "Extra Co", extra="").replace(
        b"name: Extra Co\n", b"name: Extra Co\nclient: extra-field\n")
    write(library_root, "Extra/Client.md", data)
    entry = next(e for e in lib.discover().entries if e.id == "extra-co")
    assert entry.client is None
    row = next(r for r in lib.find("extra").results if r["id"] == "extra-co")
    assert row["client"] is None
    assert lib.resolve("extra-co").candidate()["client"] is None
    from engagements.records import parse

    assert parse((library_root / "Extra/Client.md").read_bytes()).properties["client"] == "extra-field"


def test_projects_still_report_their_client(lib):
    row = next(r for r in lib.find("website").results if r["id"] == "acme-website-2026")
    assert row["client"] == "acme-bakery"
