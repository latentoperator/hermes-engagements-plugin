"""open_library, discovery boundaries (spec 4.1) and ensure_inside."""

import os
import threading
import uuid

import pytest
import yaml
from conftest import record_bytes, write
from engagements.errors import EngagementsError
from engagements.library import Library, ensure_inside, open_library


@pytest.fixture
def lib(opened_lib):
    return opened_lib

SECRET = "AKIAFAKEFAKEFAKE1234"


def code(excinfo):
    return excinfo.value.code


# --- open_library ------------------------------------------------------------


def test_open_existing_library_reads_the_fixture_id(library_root):
    lib = open_library(library_root, create_default=False)
    assert isinstance(lib, Library)
    assert lib.library_id == "11111111-1111-4111-8111-111111111111"
    assert lib.root == library_root.resolve()
    assert lib.created is False


def test_root_is_resolved_once_through_symlinks(library_root, tmp_path):
    alias = tmp_path / "alias"
    alias.symlink_to(library_root)
    lib = open_library(alias, create_default=False)
    assert lib.root == library_root.resolve()


def test_missing_root_without_create_default_is_library_missing(tmp_path):
    with pytest.raises(EngagementsError) as e:
        open_library(tmp_path / "nope", create_default=False)
    assert code(e) == "library_missing"


def test_root_without_library_yaml_is_library_missing_when_not_default(tmp_path):
    (tmp_path / "notes").mkdir()
    with pytest.raises(EngagementsError) as e:
        open_library(tmp_path / "notes", create_default=False)
    assert code(e) == "library_missing"
    assert not (tmp_path / "notes" / ".engagements").exists()


@pytest.mark.parametrize("content", [
    "", "format: 2\nlibrary_id: 11111111-1111-4111-8111-111111111111\n",
    "format: 1\n", "format: 1\nlibrary_id: not-a-uuid\n", "- a\n- b\n", "format: [1\n",
    "format: true\nlibrary_id: 11111111-1111-4111-8111-111111111111\n",
])
def test_invalid_library_yaml_is_library_missing_and_is_never_overwritten(library_root, content):
    path = library_root / ".engagements" / "library.yaml"
    path.write_text(content)
    for create_default in (False, True):
        with pytest.raises(EngagementsError) as e:
            open_library(library_root, create_default=create_default)
        assert code(e) == "library_missing"
    assert path.read_text() == content


def test_file_as_root_is_library_missing(tmp_path):
    (tmp_path / "file").write_text("x")
    with pytest.raises(EngagementsError) as e:
        open_library(tmp_path / "file", create_default=True)
    assert code(e) == "library_missing"


def test_first_use_of_an_empty_default_root_creates_the_library(tmp_path):
    root = tmp_path / "deep" / "library"
    lib = open_library(root, create_default=True)
    assert lib.created is True
    meta = root / ".engagements"
    for sub in ("locks", "history", "snapshots"):
        assert (meta / sub).is_dir()
    data = yaml.safe_load((meta / "library.yaml").read_text())
    assert data["format"] == 1
    assert str(uuid.UUID(data["library_id"])) == data["library_id"]
    assert lib.library_id == data["library_id"]
    assert lib.base_warnings()[0]["code"] == "library_created"


def test_second_open_reuses_the_library_and_does_not_report_creation(tmp_path):
    root = tmp_path / "library"
    first = open_library(root, create_default=True)
    second = open_library(root, create_default=True)
    assert second.library_id == first.library_id
    assert second.created is False
    assert second.base_warnings() == []


def test_library_ids_are_random(tmp_path):
    a = open_library(tmp_path / "a", create_default=True)
    b = open_library(tmp_path / "b", create_default=True)
    assert a.library_id != b.library_id


def test_concurrent_first_use_creates_exactly_once(tmp_path):
    root = tmp_path / "library"
    results = []
    barrier = threading.Barrier(8)

    def go():
        barrier.wait()
        results.append(open_library(root, create_default=True))

    threads = [threading.Thread(target=go) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 8
    assert sum(r.created for r in results) == 1
    assert len({r.library_id for r in results}) == 1


def test_symlinked_metadata_directory_is_refused_with_io_error(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (root / ".engagements").symlink_to(elsewhere)
    with pytest.raises(EngagementsError) as e:
        open_library(root, create_default=True)
    assert code(e) == "io_error"
    assert list(elsewhere.iterdir()) == []


def test_symlinked_library_yaml_is_not_followed(library_root, tmp_path):
    target = tmp_path / "real.yaml"
    target.write_text("format: 1\nlibrary_id: 22222222-2222-4222-8222-222222222222\n")
    path = library_root / ".engagements" / "library.yaml"
    path.unlink()
    path.symlink_to(target)
    with pytest.raises(EngagementsError) as e:
        open_library(library_root, create_default=False)
    assert code(e) in {"library_missing", "io_error"}


# --- discovery -------------------------------------------------------------


def ids(lib):
    return sorted(entry.id for entry in lib.discover().entries)


def test_fixture_library_is_discovered(lib):
    d = lib.discover()
    assert sorted(e.relpath for e in d.entries) == [
        "Acme Bakery/Client.md",
        "Acme Bakery/Projects/Website 2026.md",
        "Riverbend Dental/Client.md",
        "Riverbend Dental/Projects/Brand Assets.md",
        "Riverbend Dental/Projects/Recall Campaign.md",
    ]
    assert d.skipped_symlinks == [] and d.oversize == []
    acme = next(e for e in d.entries if e.id == "acme-bakery")
    assert (acme.kind, acme.name, acme.status, acme.client, acme.valid) == ("client", "Acme Bakery", "active", None, True)
    site = next(e for e in d.entries if e.id == "acme-website-2026")
    assert (site.kind, site.client, site.relpath) == ("project", "acme-bakery", "Acme Bakery/Projects/Website 2026.md")


def test_discovery_order_is_deterministic(lib):
    first = [e.relpath for e in lib.discover().entries]
    second = [e.relpath for e in lib.discover().entries]
    assert first == second == sorted(first)


def test_files_without_the_marker_are_never_records(lib, library_root):
    write(library_root, "Notes/plain.md", "# Just a note\nSome text\n")
    write(library_root, "Notes/other-fm.md", "---\ntitle: hi\n---\nbody\n")
    write(library_root, "Notes/engagements-in-body.md", "# T\nengagements: client\nid: sneaky\n")
    write(library_root, "Notes/wrong-value.md", "---\nengagements: vendor\nid: vendor-one\nname: V\n---\n")
    assert "sneaky" not in ids(lib) and "vendor-one" not in ids(lib)
    assert len(lib.discover().entries) == 5


def test_non_md_files_are_ignored(lib, library_root):
    write(library_root, "Acme Bakery/Client.txt", record_bytes("client", "text-client", "Text Client"))
    write(library_root, "Acme Bakery/Client.md.bak", record_bytes("client", "bak-client", "Bak Client"))
    assert "text-client" not in ids(lib) and "bak-client" not in ids(lib)


@pytest.mark.parametrize("directory", [".obsidian", ".hidden", ".engagements", ".git/sub"])
def test_dot_directories_are_never_searched(lib, library_root, directory):
    write(library_root, f"{directory}/Client.md", record_bytes("client", "hidden-client", "Hidden"))
    assert "hidden-client" not in ids(lib)


def test_metadata_snapshots_with_record_markers_are_not_records(lib, library_root):
    write(library_root, ".engagements/snapshots/0123456789abcdef.md",
          record_bytes("client", "snap-client", "Snapshot"))
    write(library_root, ".engagements/history/acme-bakery/x.md", record_bytes("client", "hist-client", "Hist"))
    assert not {"snap-client", "hist-client"} & set(ids(lib))


def test_nested_dot_directory_below_normal_directory_is_skipped(lib, library_root):
    write(library_root, "Acme Bakery/.drafts/Client.md", record_bytes("client", "draft-client", "Draft"))
    assert "draft-client" not in ids(lib)


def test_symlinked_file_and_directory_are_skipped_and_reported(lib, library_root, tmp_path):
    outside = tmp_path / "outside"
    write(outside, "Client.md", record_bytes("client", "outside-client", "Outside"))
    write(outside, "linked-dir/Client.md", record_bytes("client", "linked-client", "Linked"))
    os.symlink(outside / "Client.md", library_root / "Linked File.md")
    os.symlink(outside / "linked-dir", library_root / "Linked Dir")
    os.symlink(library_root / "Acme Bakery" / "Client.md", library_root / "Inside Link.md")
    d = lib.discover()
    assert not {"outside-client", "linked-client"} & {e.id for e in d.entries}
    assert sum(e.id == "acme-bakery" for e in d.entries) == 1  # a link inside the root is not a second record
    assert sorted(d.skipped_symlinks) == ["Inside Link.md", "Linked Dir", "Linked File.md"]


def test_symlinked_dot_directory_is_silently_skipped(lib, library_root, tmp_path):
    outside = tmp_path / "outside"
    write(outside, "Client.md", record_bytes("client", "outside-client", "Outside"))
    os.symlink(outside, library_root / ".link")
    d = lib.discover()
    assert "outside-client" not in {e.id for e in d.entries}


def _spy_reads(monkeypatch):
    """Record (file size, bytes returned) for every os.read made by the library."""
    reads = []
    real_read = os.read

    def spy(fd, n):
        data = real_read(fd, n)
        reads.append((os.fstat(fd).st_size, len(data)))
        return data

    monkeypatch.setattr("engagements.library.os.read", spy)
    return reads


def test_files_over_one_mebibyte_are_skipped_without_being_read(lib, library_root, monkeypatch):
    big = record_bytes("client", "big-client", "Big") + b"x" * (1024 * 1024)
    write(library_root, "Big/Client.md", big)
    exactly = bytearray(record_bytes("client", "edge-client", "Edge"))
    exactly += b"y" * (1024 * 1024 - len(exactly))
    assert len(exactly) == 1024 * 1024
    write(library_root, "Edge/Client.md", bytes(exactly))
    reads = _spy_reads(monkeypatch)
    d = lib.discover()
    found = {e.id for e in d.entries}
    assert "big-client" not in found
    assert "edge-client" in found  # exactly 1 MiB is still allowed
    assert d.oversize == [("Big/Client.md", len(big))]
    assert not [r for r in reads if r[0] == len(big)]


def test_non_record_note_is_never_read_past_64_kib(lib, library_root, monkeypatch):
    note = ("# Credentials\n" + "padding line\n" * 6000 + f"password = {SECRET}\n").encode()
    assert 65536 < len(note) < 1024 * 1024
    write(library_root, "Notes/credentials.md", note)
    reads = _spy_reads(monkeypatch)
    d = lib.discover()
    assert sum(n for size, n in reads if size == len(note)) <= 65536
    assert SECRET not in repr(d) and SECRET not in repr(lib.find("").data())


def test_marker_after_64_kib_is_not_a_record(lib, library_root):
    padding = "# note\n" + "z: " + "a" * 70000 + "\n"
    data = "---\nengagements: client\nid: late-client\nname: Late\n" + padding + "---\n"
    write(library_root, "Late/Client.md", data)
    assert "late-client" not in ids(lib)


def test_frontmatter_closing_line_straddling_64_kib_is_not_a_record(lib, library_root):
    head = "---\nengagements: client\nid: straddle\nname: Straddle\nnote: "
    fill = 65536 - len(head) - 3  # the closing '---' then starts inside the window but ends after it
    data = head + "a" * fill + "\n---\n\n## Summary\n"
    write(library_root, "Straddle/Client.md", data)
    assert "straddle" not in ids(lib)


def test_record_larger_than_64_kib_is_still_found_and_fully_parsed(lib, library_root):
    data = record_bytes("client", "long-client", "Long Client") + b"filler line\n" * 8000
    assert len(data) > 65536
    write(library_root, "Long/Client.md", data)
    entry = next(e for e in lib.discover().entries if e.id == "long-client")
    assert entry.valid and entry.name == "Long Client"


def test_multibyte_character_split_by_the_window_does_not_make_a_record_malformed(lib, library_root):
    body = "é" * 40000  # straddles 65,536 on a boundary that may split a code point
    data = record_bytes("client", "accent-client", "Accent Client") + body.encode()
    write(library_root, "Accent/Client.md", data)
    entry = next(e for e in lib.discover().entries if e.id == "accent-client")
    assert entry.valid


def test_bom_record_is_found(lib, library_root):
    write(library_root, "Bom/Client.md", b"\xef\xbb\xbf" + record_bytes("client", "bom-client", "Bom Client"))
    entry = next(e for e in lib.discover().entries if e.id == "bom-client")
    assert entry.valid


def test_unreadable_files_are_reported_not_raised(lib, library_root):
    path = write(library_root, "Locked/Client.md", record_bytes("client", "locked-client", "Locked"))
    path.chmod(0)
    try:
        if os.access(path, os.R_OK):
            pytest.skip("running with privileges that ignore file modes")
        d = lib.discover()
        assert d.unreadable == ["Locked/Client.md"]
        assert "locked-client" not in {e.id for e in d.entries}
    finally:
        path.chmod(0o644)


def test_unreadable_directory_is_reported_not_raised(lib, library_root):
    sub = library_root / "Sealed"
    sub.mkdir()
    write(sub, "Client.md", record_bytes("client", "sealed", "Sealed"))
    sub.chmod(0)
    try:
        if os.access(sub, os.R_OK):
            pytest.skip("running with privileges that ignore file modes")
        assert lib.discover().unreadable == ["Sealed"]
    finally:
        sub.chmod(0o755)


def test_malformed_yaml_record_is_found_with_line_scanned_identity(lib, library_root):
    data = b"---\nengagements: client\nid: broken-client\nname: \"Broken Client\"\naliases: [Oops\n---\n\n## Summary\n"
    write(library_root, "Broken/Client.md", data)
    entry = next(e for e in lib.discover().entries if e.id == "broken-client")
    assert entry.valid is False and entry.kind == "client" and entry.name == "Broken Client"
    assert "yaml_parse" in entry.defects


def test_malformed_yaml_record_without_id_has_null_identity(lib, library_root):
    data = b"---\nengagements: project\naliases: [Oops\n---\n"
    write(library_root, "Broken/Project.md", data)
    entry = next(e for e in lib.discover().entries if e.relpath == "Broken/Project.md")
    assert entry.valid is False and entry.kind == "project" and entry.id is None and entry.name is None


def test_duplicate_ids_are_library_defects_on_both_records(lib, library_root):
    write(library_root, "Copy/Client.md", record_bytes("client", "acme-bakery", "Acme Copy"))
    d = lib.discover()
    dupes = [e for e in d.entries if e.id == "acme-bakery"]
    assert len(dupes) == 2
    assert all("duplicate_id" in e.defects and not e.valid for e in dupes)


def test_unknown_client_is_a_library_defect(lib, library_root):
    write(library_root, "Orphan/Projects/Orphan.md",
          record_bytes("project", "orphan-project", "Orphan", client="ghost-client"))
    write(library_root, "Orphan/Projects/Wrong.md",
          record_bytes("project", "wrong-project", "Wrong", client="acme-website-2026"))
    d = lib.discover()
    assert "unknown_client" in next(e for e in d.entries if e.id == "orphan-project").defects
    assert "unknown_client" in next(e for e in d.entries if e.id == "wrong-project").defects
    assert next(e for e in d.entries if e.id == "acme-website-2026").valid


def test_library_defects_helper_for_the_writer(lib, library_root):
    from engagements.records import parse

    write(library_root, "Orphan/Projects/Orphan.md",
          record_bytes("project", "orphan-project", "Orphan", client="ghost-client"))
    write(library_root, "Copy/Client.md", record_bytes("client", "acme-bakery", "Acme Copy"))
    d = lib.discover()
    orphan = parse((library_root / "Orphan/Projects/Orphan.md").read_bytes())
    assert lib.library_defects(orphan, "Orphan/Projects/Orphan.md", discovery=d) == {"unknown_client"}
    copy = parse((library_root / "Copy/Client.md").read_bytes())
    assert lib.library_defects(copy, "Copy/Client.md", discovery=d) == {"duplicate_id"}
    site = parse((library_root / "Acme Bakery/Projects/Website 2026.md").read_bytes())
    assert lib.library_defects(site, "Acme Bakery/Projects/Website 2026.md", discovery=d) == set()


def test_repeated_marker_with_conflicting_kinds_has_null_kind(lib, library_root):
    data = b"---\nengagements: client\nid: conflict-one\nname: C\nengagements: project\n---\n"
    write(library_root, "Conflict/Client.md", data)
    entry = next(e for e in lib.discover().entries if e.relpath == "Conflict/Client.md")
    assert entry.kind is None and entry.valid is False


def test_alias_and_status_are_exposed(lib, library_root):
    write(library_root, "Zed/Client.md", record_bytes("client", "zed-co", "Zed Co", aliases=["Zed", "Z Co"], status="paused"))
    entry = next(e for e in lib.discover().entries if e.id == "zed-co")
    assert entry.aliases == ("Zed", "Z Co") and entry.status == "paused"


def test_missing_status_defaults_to_active(lib, library_root):
    write(library_root, "Zed/Client.md", "---\nengagements: client\nid: zed-co\nname: Zed Co\n---\n")
    assert next(e for e in lib.discover().entries if e.id == "zed-co").status == "active"


# --- ensure_inside -----------------------------------------------------------


def test_ensure_inside_accepts_relative_and_absolute_paths_inside(lib):
    inside = lib.root / "Acme Bakery" / "Client.md"
    assert ensure_inside(lib.root, "Acme Bakery/Client.md") == inside
    assert ensure_inside(lib.root, inside) == inside
    assert ensure_inside(lib.root, lib.root) == lib.root
    assert ensure_inside(lib.root, "Does Not Exist Yet/New.md") == lib.root / "Does Not Exist Yet" / "New.md"


@pytest.mark.parametrize("path", ["../escape.md", "Acme Bakery/../../escape.md", "/etc/passwd", "/", "a/../../../x"])
def test_ensure_inside_refuses_escapes(lib, path):
    with pytest.raises(EngagementsError) as e:
        ensure_inside(lib.root, path)
    assert code(e) == "outside_root"


def test_ensure_inside_refuses_a_sibling_with_the_same_prefix(lib):
    sibling = lib.root.parent / (lib.root.name + "-evil")
    sibling.mkdir()
    with pytest.raises(EngagementsError) as e:
        ensure_inside(lib.root, sibling / "x.md")
    assert code(e) == "outside_root"


def test_ensure_inside_refuses_a_symlink_pointing_outside(lib, library_root, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, library_root / "Escape")
    os.symlink(outside / "file.md", library_root / "EscapeFile.md")
    for path in ("Escape/new.md", "Escape", "EscapeFile.md", library_root / "Escape" / "x"):
        with pytest.raises(EngagementsError) as e:
            ensure_inside(lib.root, path)
        assert code(e) == "outside_root"


def test_ensure_inside_allows_a_symlink_that_stays_inside(lib, library_root):
    os.symlink(library_root / "Acme Bakery", library_root / "Shortcut")
    assert ensure_inside(lib.root, "Shortcut/Client.md") == lib.root / "Acme Bakery" / "Client.md"


def test_ensure_inside_rejects_null_bytes_with_invalid_op(lib):
    with pytest.raises(EngagementsError) as e:
        ensure_inside(lib.root, "a\0b")
    assert code(e) == "invalid_op"


def test_ensure_inside_accepts_a_library_object(lib):
    assert ensure_inside(lib, "Acme Bakery/Client.md") == lib.root / "Acme Bakery" / "Client.md"


# --- path-based reads: record_path ------------------------------------------


def test_record_path_returns_the_relative_slash_path(lib):
    assert lib.record_path("Acme Bakery/Client.md") == "Acme Bakery/Client.md"
    assert lib.record_path(lib.root / "Acme Bakery" / "Projects" / "Website 2026.md") == "Acme Bakery/Projects/Website 2026.md"


def test_record_path_escape_is_outside_root(lib):
    with pytest.raises(EngagementsError) as e:
        lib.record_path("../x.md")
    assert code(e) == "outside_root"


@pytest.mark.parametrize("path", [".engagements/library.yaml", ".obsidian/x.md", "Acme Bakery/.drafts/x.md"])
def test_record_path_in_a_dot_directory_is_excluded(lib, path):
    with pytest.raises(EngagementsError) as e:
        lib.record_path(path)
    assert code(e) == "not_found" and e.value.details["reason"] == "excluded"


def test_record_path_behind_a_symlink_component_is_excluded(lib, library_root):
    os.symlink(library_root / "Acme Bakery", library_root / "Shortcut")
    with pytest.raises(EngagementsError) as e:
        lib.record_path("Shortcut/Client.md")
    assert code(e) == "not_found" and e.value.details["reason"] == "excluded"


def test_record_path_requires_a_markdown_file(lib):
    with pytest.raises(EngagementsError) as e:
        lib.record_path("Acme Bakery/Client.txt")
    assert code(e) == "not_found" and e.value.details["reason"] == "not_a_record"


# --- readings of the spec taken conservatively ---------------------------------


def test_a_dot_named_markdown_file_in_a_normal_directory_is_still_a_record(lib, library_root):
    # SPEC-QUESTION: spec 4.1 excludes files "inside any dot-directory", not dot-named files.
    # Read literally, ".hidden.md" in a normal directory is a record; only directories are skipped.
    write(library_root, "Acme Bakery/.hidden.md", record_bytes("client", "dotfile-client", "Dotfile"))
    assert "dotfile-client" in ids(lib)


def test_markdown_suffix_is_matched_case_sensitively(lib, library_root):
    # SPEC-QUESTION: spec 4.1 says "regular `.md` file"; the conservative reading is the exact
    # lowercase suffix, so Client.MD is not discovered.
    write(library_root, "Upper/Client.MD", record_bytes("client", "upper-client", "Upper"))
    assert "upper-client" not in ids(lib)


def test_targeted_defects_make_a_record_invalid_without_hiding_it(lib, library_root):
    data = record_bytes("client", "dup-sections", "Dup Sections") + b"\n## Summary\nagain\n"
    write(library_root, "Dup/Client.md", data)
    entry = next(e for e in lib.discover().entries if e.id == "dup-sections")
    assert entry.valid is False and "duplicate_section" in entry.defects
