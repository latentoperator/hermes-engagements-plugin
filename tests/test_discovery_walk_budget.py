"""The walk counts what it lists and stops at a documented limit.

Two numbers bound a walk (``engagements/schema.py``): above the warning bound
``doctor`` says the root is very large, and above the limit the walk stops and
discovery is marked incomplete. An incomplete inventory proves nothing about what
it did not reach: ``create`` refuses, automatic binding refuses as
``discovery_incomplete``, ``find`` answers with what it read and a warning, and an
explicit read or bind by id still works for a record that was seen. Nothing here
depends on time, only on how many directory entries were listed.
"""

import contextlib
import json

import pytest
from conftest import record_bytes, write
from engagements import doctor as doctor_module
from engagements import library as library_module
from engagements.library import open_library
from engagements.schema import WALK_ENTRY_LIMIT, WALK_ENTRY_WARNING
from tools_support import call, make_session


@pytest.fixture(autouse=True)
def isolated_cache(monkeypatch):
    monkeypatch.setattr(library_module, "_SCAN_CACHE", library_module._ScanCache())


@pytest.fixture
def lib(tmp_path):
    lib = open_library(tmp_path / "library", create_default=True)
    for index in range(6):
        write(lib.root, f"Client {index}/Client.md",
              record_bytes("client", f"client-{index}", f"Client {index}"))
    return lib


def test_the_documented_bounds_are_the_ones_that_ship():
    assert (WALK_ENTRY_WARNING, WALK_ENTRY_LIMIT) == (20_000, 200_000)


def test_the_walk_counts_every_entry_it_lists(lib):
    discovery = lib.discover()
    # Six client folders and their six records, plus the root's dot-directory.
    assert discovery.listed == 1 + 6 + 6
    assert discovery.incomplete is False


def test_a_walk_that_stops_at_the_limit_is_incomplete_and_partial(lib, monkeypatch):
    monkeypatch.setattr(library_module, "WALK_ENTRY_LIMIT", 10)
    discovery = lib.discover()
    assert discovery.incomplete is True
    assert 0 < len(discovery.entries) < 6, "the walk must stop where the limit is, not finish and then complain"


def test_find_answers_with_what_it_read_and_says_so(lib, monkeypatch):
    monkeypatch.setattr(library_module, "WALK_ENTRY_LIMIT", 10)
    result = call("engagements_find", {"query": "", "limit": 100}, make_session(lib.root)[0])
    assert result["ok"] is True, result
    assert 0 < result["data"]["total"] < 6
    assert [warning["code"] for warning in result["warnings"]] == ["discovery_incomplete"]
    assert "too large" in result["warnings"][0]["message"]


def test_create_refuses_because_uniqueness_cannot_be_proven(lib, monkeypatch):
    monkeypatch.setattr(library_module, "WALK_ENTRY_LIMIT", 4)
    result = call("engagements_create", {"kind": "client", "name": "Fictional Newcomer"},
                  make_session(lib.root)[0])
    assert result["ok"] is False, result
    assert result["error"]["code"] == "io_error"
    assert result["error"]["details"]["reason"] == "incomplete_scan"
    assert not (lib.root / "Fictional Newcomer").exists()


@pytest.fixture
def matched_library(tmp_path):
    """One project listing the working folder, and a second folder the walk never reaches."""
    work = tmp_path / "work"
    work.mkdir()
    lib = open_library(tmp_path / "library", create_default=True)
    write(lib.root, "A Client/Client.md", record_bytes("client", "client-a", "Client A"))
    write(lib.root, "A Client/Projects/Website.md",
          b"---\nengagements: project\nid: project-a\nname: Website\nclient: client-a\n"
          b"status: active\nlocations: [%s]\nupdated: 2026-11-05\n---\n\n## Summary\n\nFictional.\n"
          % str(work).encode("utf-8"))
    for index in range(5):
        write(lib.root, f"Z Notes/{index}.md", b"not a record\n")
    return lib, work


def test_automatic_binding_refuses_as_discovery_incomplete(matched_library, monkeypatch):
    lib, work = matched_library
    monkeypatch.setattr(library_module, "WALK_ENTRY_LIMIT", 9)
    result = call("engagements_bind", {"mode": "auto"},
                  make_session(lib.root, cwd=str(work))[0])
    assert result["ok"] is True, result
    assert result["data"]["state"] == "unbound"
    assert result["data"]["unreadable"] == [library_module.ROOT_RELPATH]


def test_an_explicit_read_by_id_still_works_for_a_record_that_was_seen(matched_library, monkeypatch):
    lib, _work = matched_library
    monkeypatch.setattr(library_module, "WALK_ENTRY_LIMIT", 9)
    discovery = lib.discover()
    assert discovery.incomplete is True and [e.id for e in discovery.entries] == ["client-a", "project-a"]
    result = call("engagements_context", {"id": "project-a"}, make_session(lib.root)[0])
    assert result["ok"] is True, result
    assert result["data"]["id"] == "project-a"


def test_doctor_warns_above_the_warning_bound_and_still_reports(lib, monkeypatch):
    monkeypatch.setattr(doctor_module, "WALK_ENTRY_WARNING", 3)
    report = doctor_module.run_doctor(lib.root)
    codes = [finding.code for finding in report.findings]
    assert "large_library" in codes
    assert report.findings[[finding.code for finding in report.findings].index("large_library")].severity == "warning"
    assert "incomplete_scan" not in codes


def test_doctor_reports_a_root_it_could_not_finish_as_an_error(lib, monkeypatch):
    monkeypatch.setattr(library_module, "WALK_ENTRY_LIMIT", 4)
    monkeypatch.setattr(doctor_module, "WALK_ENTRY_LIMIT", 4)
    report = doctor_module.run_doctor(lib.root)
    errors = [finding for finding in report.findings if finding.severity == "error"]
    assert [finding.code for finding in errors] == ["incomplete_scan"]
    assert errors[0].path == "."
    assert not report.clean


def test_doctor_says_nothing_about_the_cache(lib):
    report = doctor_module.run_doctor(lib.root)
    assert "cache" not in json.dumps([finding.as_dict() for finding in report.findings]).lower()


class _Child:
    """One directory entry of a synthetic stream: never a link, never a directory."""

    def __init__(self, index):
        self.name = f"entry-{index:08}.txt"

    def is_symlink(self):
        return False

    def is_dir(self, **kwargs):
        return False

    def is_file(self, **kwargs):
        return False


class _Directory:
    """A directory stream that counts how many of its entries were pulled from it."""

    def __init__(self, count):
        self.count = count
        self.consumed = 0

    @contextlib.contextmanager
    def scandir(self):
        def supplied():
            for index in range(self.count):
                self.consumed += 1
                yield _Child(index)

        yield supplied()


def test_a_directory_over_the_whole_budget_is_given_up_while_it_is_being_read():
    """The production limit, with a stream that would happily supply more than it.

    A listing is bounded as it is consumed: the walk buffers only what it may still
    list, reads one entry more to learn that the directory does not fit, and then
    drops the directory whole. Nothing here is read to the end and sorted first.
    """
    directory = _Directory(WALK_ENTRY_LIMIT + 50_000)
    discovery = library_module.Discovery()
    library_module._walk(directory, "", discovery)
    assert directory.consumed == WALK_ENTRY_LIMIT + 1
    assert discovery.incomplete is True
    assert discovery.entries == []
    assert discovery.listed == WALK_ENTRY_LIMIT + 1


def test_a_walk_cut_inside_one_directory_still_refuses_a_write_and_a_binding(tmp_path, monkeypatch):
    """A directory the budget cannot finish is dropped, and fail-closed consumers refuse."""
    lib = open_library(tmp_path / "library", create_default=True)
    work = tmp_path / "work"
    work.mkdir()
    crowded = lib.root / "Crowded"
    crowded.mkdir()
    for index in range(30):
        write(crowded, f"{index:02}.md", b"not a record\n")
    monkeypatch.setattr(library_module, "WALK_ENTRY_LIMIT", 10)
    discovery = lib.discover()
    assert discovery.incomplete is True
    assert discovery.entries == [], "the partial listing of the cut-off directory was used"
    assert discovery.listed <= 11
    created = call("engagements_create", {"kind": "client", "name": "Fictional Newcomer"},
                   make_session(lib.root)[0])
    assert created["ok"] is False, created
    assert created["error"]["details"]["reason"] == "incomplete_scan"
    assert not (lib.root / "Fictional Newcomer").exists()
    bound = call("engagements_bind", {"mode": "auto"}, make_session(lib.root, cwd=str(work))[0])
    assert bound["ok"] is True, bound
    assert bound["data"]["state"] == "unbound"
    assert bound["data"]["client"] is None and bound["data"]["project"] is None
