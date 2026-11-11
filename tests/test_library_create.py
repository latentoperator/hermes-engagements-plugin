"""engagements_create behavior: validation, similarity, ids, filenames, placement, publication."""

import errno
import os
import re
import sys
from datetime import date, datetime

import pytest
from conftest import TZ, clock, patch_link, record_bytes, write
from engagements import library as library_module
from engagements.errors import EngagementsError
from engagements.fsutil import FileLock, rev
from engagements.records import parse


@pytest.fixture
def lib(opened_lib):
    return opened_lib

ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{1,62}")


def make(lib, kind="client", name="Foo Co", **kwargs):
    kwargs.setdefault("profile", "studio-dev")
    kwargs.setdefault("today", clock)
    return lib.create(kind, name, **kwargs)


def fails(lib, code, **kwargs):
    with pytest.raises(EngagementsError) as e:
        make(lib, **kwargs)
    assert e.value.code == code, (e.value.code, e.value.details)
    return e.value


def tree(root):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if ".engagements" not in p.parts)


# --- TOOLS.md examples -------------------------------------------------------


def test_tools_md_project_success_example(lib, library_root):
    result = make(lib, "project", "Holiday Postcards 2026", client="riverbend-dental",
                  properties={"locations": ["~/clients/riverbend/postcards-2026"]},
                  source="kickoff call 2026-11-06")
    path = library_root / "Riverbend Dental/Projects/Holiday Postcards 2026.md"
    assert result.data() == {"id": "holiday-postcards-2026", "kind": "project",
                             "path": "Riverbend Dental/Projects/Holiday Postcards 2026.md",
                             "rev": rev(path.read_bytes()), "similar_overridden": []}
    assert result.warnings == []
    record = parse(path.read_bytes())
    assert record.global_defects == set() and record.kind == "project"
    assert record.properties["locations"] == ["~/clients/riverbend/postcards-2026"]
    assert record.properties["client"] == "riverbend-dental"
    text = path.read_text()
    assert text.rstrip("\n").endswith("- 2026-11-07 studio-dev: created project (source: kickoff call 2026-11-06)")


def test_tools_md_similar_exists_error_example(lib):
    err = fails(lib, "similar_exists", name="ACME bakery")
    assert err.details == {"candidates": [{"id": "acme-bakery", "kind": "client", "name": "Acme Bakery",
                                           "client": None, "path": "Acme Bakery/Client.md"}]}
    assert "ACME bakery" in err.message and "allow_similar" in err.message


# --- the written record ------------------------------------------------------


def test_client_is_written_from_the_template(lib, library_root):
    result = make(lib, "client", "Foo Co", properties={"aliases": ["Foo"]}, sections=[
        {"section": "Summary", "body": "Two-person shop."},
        {"section": "Pricing notes", "body": "Net 30."},
    ])
    assert result.path == "Foo Co/Client.md"
    data = (library_root / "Foo Co/Client.md").read_bytes()
    record = parse(data)
    assert record.global_defects == set()
    assert record.properties == {"engagements": "client", "id": "foo-co", "name": "Foo Co",
                                 "aliases": ["Foo"], "status": "active", "updated": "2026-11-07"}
    assert [s.key for s in record.sections] == [
        "summary", "current work", "contacts", "communication preferences", "locations",
        "systems of record", "history", "unconfirmed", "pricing notes", "change log"]
    text = data.decode()
    assert "## Summary\nTwo-person shop.\n" in text and "## Pricing notes\nNet 30.\n\n## Change log" in text
    assert text.endswith("- 2026-11-07 studio-dev: created client (source: conversation)\n")


def test_templates_never_carry_the_illustrative_spec_content(lib, library_root):
    make(lib, "client", "Foo Co")
    text = (library_root / "Foo Co/Client.md").read_text()
    assert "Jane Rivera" not in text and "acme-bakery.example" not in text


def test_project_defaults(lib, library_root):
    make(lib, "project", "Menu Refresh", client="acme-bakery")
    record = parse((library_root / "Acme Bakery/Projects/Menu Refresh.md").read_bytes())
    assert record.properties["status"] == "active" and record.properties["locations"] == []
    assert record.properties["aliases"] == []
    assert [s.key for s in record.sections] == ["purpose", "notes", "change log"]


def test_aliases_are_deduplicated_by_normalized_text_first_kept(lib, library_root):
    make(lib, "client", "Foo Co", properties={"aliases": ["Foo", "FOO", "foo!", "Bar"]})
    assert parse((library_root / "Foo Co/Client.md").read_bytes()).properties["aliases"] == ["Foo", "Bar"]


def test_status_and_source_are_written(lib, library_root):
    make(lib, "client", "Foo Co", properties={"status": "paused"}, source="owner email (Nov 7)")
    text = (library_root / "Foo Co/Client.md").read_text()
    assert "status: paused" in text and text.rstrip().endswith("(source: owner email (Nov 7))")


def test_created_file_has_no_temp_leftovers(lib, library_root):
    make(lib, "client", "Foo Co")
    make(lib, "project", "Menu Refresh", client="foo-co")
    leftovers = [p for p in library_root.rglob("*") if p.name.startswith(".engagements-")]
    assert leftovers == []


def test_clock_may_be_a_fixed_datetime_or_date(lib, library_root):
    make(lib, "client", "Foo Co", today=datetime(2026, 12, 25, 1, 0, tzinfo=TZ))
    assert "- 2026-12-25 studio-dev: created" in (library_root / "Foo Co/Client.md").read_text()
    make(lib, "client", "Bar Co", today=date(2027, 1, 2))
    assert "- 2027-01-02 studio-dev: created" in (library_root / "Bar Co/Client.md").read_text()


def test_profile_defaults_to_default(lib, library_root):
    lib.create("client", "Foo Co", profile=None, today=clock)
    assert "- 2026-11-07 default: created client" in (library_root / "Foo Co/Client.md").read_text()


def test_balanced_fence_containing_heading_lines_is_allowed(lib, library_root):
    body = "Example:\n```\n## not a heading\n# also not\n```"
    make(lib, "client", "Foo Co", sections=[{"section": "Summary", "body": body}])
    record = parse((library_root / "Foo Co/Client.md").read_bytes())
    assert [s.key for s in record.sections].count("summary") == 1


def test_crlf_in_a_body_is_normalized(lib, library_root):
    make(lib, "client", "Foo Co", sections=[{"section": "Summary", "body": "a\r\nb\r\n\r\n"}])
    data = (library_root / "Foo Co/Client.md").read_bytes()
    assert b"\r" not in data and b"## Summary\na\nb\n\n" in data


# --- placement ---------------------------------------------------------------


def test_default_placement(lib):
    assert make(lib, "client", "Foo Co").path == "Foo Co/Client.md"
    assert make(lib, "project", "Menu Refresh", client="foo-co").path == "Foo Co/Projects/Menu Refresh.md"


def test_project_goes_under_its_clients_record_directory_wherever_it_lives(lib, library_root):
    (library_root / "Acme Bakery").rename(library_root / "Moved")
    (library_root / "Moved/Client.md").rename(library_root / "Moved/Acme.md")
    assert make(lib, "project", "Menu Refresh", client="acme-bakery").path == "Moved/Projects/Menu Refresh.md"


def test_project_of_a_client_record_at_the_root_goes_under_root_projects(lib, library_root):
    write(library_root, "Solo.md", record_bytes("client", "solo-co", "Solo Co"))
    assert make(lib, "project", "Menu Refresh", client="solo-co").path == "Projects/Menu Refresh.md"


def test_existing_projects_directory_is_reused_with_its_spelling(lib, library_root):
    (library_root / "Acme Bakery/Projects").rename(library_root / "Acme Bakery/projects")
    assert make(lib, "project", "Menu Refresh", client="acme-bakery").path == "Acme Bakery/projects/Menu Refresh.md"
    assert [p.name for p in (library_root / "Acme Bakery").iterdir() if p.name.lower() == "projects"] == ["projects"]


@pytest.mark.skipif(sys.platform == "win32", reason="Windows file names are case-insensitive: PROJECTS and Projects cannot coexist")
def test_ambiguous_projects_directories_are_io_error_and_nothing_is_written(lib, library_root):
    (library_root / "Acme Bakery/PROJECTS").mkdir()
    before = tree(library_root)
    err = fails(lib, "io_error", kind="project", name="Menu Refresh", client="acme-bakery")
    assert not os.path.isabs(err.details["path"])
    assert tree(library_root) == before


@pytest.mark.parametrize("kind", ["file", "symlink"])
def test_projects_entry_that_is_not_a_real_directory_is_io_error(lib, library_root, tmp_path, kind):
    target = library_root / "Riverbend Dental/Projects"
    for child in target.iterdir():
        child.unlink()
    target.rmdir()
    if kind == "file":
        target.write_text("not a directory")
    else:
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        target.symlink_to(elsewhere)
    fails(lib, "io_error", kind="project", name="Menu Refresh", client="riverbend-dental")
    if kind == "symlink":
        assert list(elsewhere.iterdir()) == []


def test_projects_directory_is_created_only_when_nothing_matches(lib, library_root):
    make(lib, "client", "Foo Co")
    assert not (library_root / "Foo Co/Projects").exists()
    make(lib, "project", "Menu Refresh", client="foo-co")
    assert (library_root / "Foo Co/Projects").is_dir()


def test_failed_project_create_does_not_leave_a_new_projects_directory(lib, library_root, monkeypatch):
    make(lib, "client", "Foo Co")

    def refuse(src, dst, **kw):
        raise OSError(errno.EACCES, "denied")

    patch_link(monkeypatch, refuse)
    fails(lib, "io_error", kind="project", name="Menu Refresh", client="foo-co")
    assert not (library_root / "Foo Co/Projects").exists()


# --- argument validation (stage 1) ------------------------------------------


@pytest.mark.parametrize(("kwargs", "field", "reason"), [
    ({"kind": "vendor"}, "kind", "invalid_value"),
    ({"name": ""}, "name", "invalid_value"),
    ({"name": "Foo\nCo"}, "name", "invalid_value"),
    ({"name": "Foo\u2028Co"}, "name", "invalid_value"),
    ({"name": "   "}, "name", "invalid_value"),
    ({"name": "x" * 201}, "name", "invalid_value"),
    ({"name": 5}, "name", "invalid_value"),
    ({"kind": "project", "client": None}, "client", "missing_field"),
    ({"kind": "project", "client": "Not An Id"}, "client", "invalid_value"),
    ({"client": "acme-bakery"}, "client", "key_not_allowed_for_kind"),
    ({"properties": "x"}, "properties", "invalid_value"),
    ({"properties": {"aliases": "Foo"}}, "properties.aliases", "invalid_value"),
    ({"properties": {"aliases": ["ok", "multi\nline"]}}, "properties.aliases", "invalid_value"),
    ({"properties": {"aliases": [""]}}, "properties.aliases", "invalid_value"),
    ({"properties": {"aliases": ["a"] * 21}}, "properties.aliases", "invalid_value"),
    ({"properties": {"locations": ["~/x"]}}, "properties.locations", "key_not_allowed_for_kind"),
    ({"properties": {"status": "bogus"}}, "properties.status", "invalid_value"),
    ({"properties": {"status": "historical"}}, "properties.status", "status_not_allowed_for_kind"),
    ({"properties": {"status": "former"}, "kind": "project", "client": "acme-bakery"},
     "properties.status", "status_not_allowed_for_kind"),
    ({"properties": {"colour": "red"}}, "properties.colour", "unexpected_field"),
    ({"kind": "project", "client": "acme-bakery", "properties": {"locations": ["a\nb"]}},
     "properties.locations", "invalid_value"),
    ({"kind": "project", "client": "acme-bakery", "properties": {"locations": ["x"] * 51}},
     "properties.locations", "invalid_value"),
    ({"properties": {"updated": "2026-01-01"}}, "properties.updated", "plugin_owned"),
    ({"sections": "x"}, "sections", "invalid_value"),
    ({"sections": [{"section": "A", "body": ""}] * 31}, "sections", "invalid_value"),
    ({"sections": ["x"]}, "sections[0]", "invalid_value"),
    ({"sections": [{"section": "A"}]}, "sections[0].body", "missing_field"),
    ({"sections": [{"body": "x"}]}, "sections[0].section", "missing_field"),
    ({"sections": [{"section": "A", "body": "x", "extra": 1}]}, "sections[0].extra", "unexpected_field"),
    ({"sections": [{"section": "Multi\nline", "body": "x"}]}, "sections[0].section", "invalid_value"),
    ({"sections": [{"section": "A", "body": "x" * 20001}]}, "sections[0].body", "invalid_value"),
    ({"sections": [{"section": "A", "body": 5}]}, "sections[0].body", "invalid_value"),
    ({"sections": [{"section": "Summary", "body": "a"}, {"section": "  SUMMARY ", "body": "b"}]},
     "sections[1].section", "duplicate_section"),
    ({"sections": [{"section": "Change log", "body": "x"}]}, "sections[0].section", "plugin_owned"),
    ({"sections": [{"section": " CHANGE   LOG ", "body": "x"}]}, "sections[0].section", "plugin_owned"),
    ({"sections": [{"section": "Summary", "body": "intro\n## Sneaky\nmore"}]}, "sections[0].body", "heading_in_text"),
    ({"sections": [{"section": "Summary", "body": "# Title"}]}, "sections[0].body", "heading_in_text"),
    ({"sections": [{"section": "Summary", "body": "ok\n```\nnever closed"}]}, "sections[0].body", "unbalanced_fence"),
    ({"sections": [{"section": "Summary", "body": "~~~\n## x"}]}, "sections[0].body", "unbalanced_fence"),
    ({"source": "line one\nline two"}, "source", "invalid_value"),
    ({"source": ""}, "source", "invalid_value"),
    ({"source": "x" * 201}, "source", "invalid_value"),
    ({"allow_similar": "yes"}, "allow_similar", "invalid_value"),
])
def test_invalid_arguments(lib, library_root, kwargs, field, reason):
    before = tree(library_root)
    err = fails(lib, "invalid_op", **kwargs)
    assert err.details["field"] == field and err.details["reason"] == reason, err.details
    assert "op_index" not in err.details
    assert tree(library_root) == before


def test_reserved_names_are_recognized_before_generic_schema_rejection(lib):
    err = fails(lib, "invalid_op", kind="vendor", sections=[{"section": "Change log", "body": "x", "extra": 1}])
    assert err.details["reason"] == "plugin_owned"


def test_section_title_that_does_not_round_trip_passes_through_structure_changed(lib, library_root):
    before = tree(library_root)
    err = fails(lib, "invalid_op", sections=[{"section": "Change log ###", "body": "x"}])
    assert err.details == {"field": "sections", "reason": "structure_changed"}
    assert tree(library_root) == before


def test_validation_errors_are_raised_before_any_filesystem_work(tmp_path):
    from engagements.library import open_library

    lib = open_library(tmp_path / "fresh", create_default=True)
    before = tree(tmp_path)
    fails(lib, "invalid_op", name="a\nb")
    assert tree(tmp_path) == before


# --- stage 4: client resolution ---------------------------------------------


def test_unknown_client_is_not_found(lib, library_root):
    before = tree(library_root)
    err = fails(lib, "not_found", kind="project", name="Menu Refresh", client="ghost-client")
    assert err.details["reason"] == "no_match"
    assert tree(library_root) == before


def test_a_project_id_is_not_a_client(lib):
    fails(lib, "not_found", kind="project", name="Menu Refresh", client="acme-website-2026")


def test_duplicated_client_id_is_ambiguous(lib, library_root):
    write(library_root, "Copy/Client.md", record_bytes("client", "acme-bakery", "Acme Copy"))
    err = fails(lib, "ambiguous", kind="project", name="Menu Refresh", client="acme-bakery")
    assert [c["path"] for c in err.details["candidates"]] == ["Acme Bakery/Client.md", "Copy/Client.md"]


def test_inactive_client_is_allowed_with_a_record_invalid_warning(lib, library_root):
    write(library_root, "Old/Client.md", record_bytes("client", "old-co", "Old Co", status="former"))
    result = make(lib, "project", "Menu Refresh", client="old-co")
    assert result.path == "Old/Projects/Menu Refresh.md"
    assert [w["code"] for w in result.warnings] == ["record_invalid"]
    assert "old-co" in result.warnings[0]["message"]


def test_unknown_client_beats_lock_timeout(lib, library_root, monkeypatch):
    monkeypatch.setattr(library_module, "LOCK_TIMEOUT", 0.1)
    lock = library_root / ".engagements" / "locks" / "_library.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(lock):
        fails(lib, "not_found", kind="project", name="Menu Refresh", client="ghost-client")
        fails(lib, "lock_timeout")


# --- similarity (spec C1) ---------------------------------------------------------


def test_similar_by_alias_and_by_name_against_alias(lib, library_root):
    make(lib, "client", "Foo Co", properties={"aliases": ["Fooey"]})
    err = fails(lib, "similar_exists", name="Totally Different", properties={"aliases": ["fooey!"]})
    assert [c["id"] for c in err.details["candidates"]] == ["foo-co"]
    err = fails(lib, "similar_exists", name="FOOEY")
    assert [c["id"] for c in err.details["candidates"]] == ["foo-co"]


def test_similarity_is_per_kind(lib):
    result = make(lib, "project", "Acme Bakery", client="acme-bakery")
    assert result.similar_overridden == []


def test_similar_projects_only_within_the_same_client(lib):
    assert make(lib, "project", "Website 2026", client="riverbend-dental").id == "website-2026"
    err = fails(lib, "similar_exists", kind="project", name="website 2026", client="acme-bakery")
    assert [c["id"] for c in err.details["candidates"]] == ["acme-website-2026"]


def test_similarity_includes_inactive_records(lib, library_root):
    write(library_root, "Old/Client.md", record_bytes("client", "old-co", "Old Co", status="former"))
    fails(lib, "similar_exists", name="Old Co")


def test_allow_similar_reports_candidates_ordered_by_name_then_id(lib, library_root):
    write(library_root, "Z/Client.md", record_bytes("client", "zeta-id", "Twin Co", aliases=["Twin"]))
    write(library_root, "A/Client.md", record_bytes("client", "alpha-id", "Twin Co"))
    write(library_root, "M/Client.md", record_bytes("client", "mid-id", "Another", aliases=["Twin Co"]))
    result = make(lib, "client", "Twin Co", allow_similar=True)
    assert [(c["name"], c["id"]) for c in result.similar_overridden] == [
        ("Another", "mid-id"), ("Twin Co", "alpha-id"), ("Twin Co", "zeta-id")]
    assert set(result.similar_overridden[0]) == {"id", "kind", "name", "client", "path"}
    err = fails(lib, "similar_exists", name="Twin Co")
    assert [c["id"] for c in err.details["candidates"]] == ["mid-id", "alpha-id", "twin-co", "zeta-id"]


def test_names_that_normalize_to_nothing_are_not_similar(lib):
    make(lib, "client", "!!!")
    assert make(lib, "client", "???").id == "client-2"


def test_malformed_records_with_names_take_part_in_similarity(lib, library_root):
    write(library_root, "Broken/Client.md", b'---\nengagements: client\nid: broken-co\nname: Broken Co\naliases: [Oops\n---\n')
    fails(lib, "similar_exists", name="Broken Co")


# --- ids (spec C4) ----------------------------------------------------------


@pytest.mark.parametrize(("name", "expected"), [
    ("Café Rouge", "cafe-rouge"), ("Straße GmbH", "strasse-gmbh"), ("X", "client-x"),
    ("!!!", "client"), ("日本", "client"), ("A.B", "a-b"), ("  --Hello   World--  ", "hello-world"),
])
def test_id_derivation_for_clients(lib, name, expected):
    assert make(lib, "client", name).id == expected


def test_id_fallback_uses_the_kind_prefix(lib):
    assert make(lib, "project", "Q", client="acme-bakery").id == "project-q"
    assert make(lib, "project", "???", client="acme-bakery").id == "project"


def test_long_name_and_collision_suffixes_stay_within_63_characters(lib):
    name = "A" + "b" * 198 + "!"
    first = make(lib, "client", name)
    second = make(lib, "client", name, allow_similar=True)
    third = make(lib, "client", name, allow_similar=True)
    assert [len(x.id) for x in (first, second, third)] == [63, 63, 63]
    assert second.id.endswith("-2") and third.id.endswith("-3")
    assert len({first.id, second.id, third.id}) == 3
    assert all(ID_PATTERN.fullmatch(x.id) for x in (first, second, third))


def test_ids_are_unique_across_clients_and_projects(lib):
    assert make(lib, "client", "Acme Website 2026").id == "acme-website-2026-2"


def test_existing_ids_are_never_reused_even_from_malformed_records(lib, library_root):
    write(library_root, "Broken/Client.md", b"---\nengagements: client\nid: foo-co\nname: Zed\naliases: [Oops\n---\n")
    assert make(lib, "client", "Foo Co").id == "foo-co-2"


# --- filenames (spec 4.4 / C6) ------------------------------------------------


def test_filename_collisions_get_numeric_suffixes_independently_of_id_suffixes(lib, library_root):
    (library_root / "Foo Co").mkdir()  # a plain directory, not a record
    first = make(lib, "client", "Foo Co")
    assert (first.id, first.path) == ("foo-co", "Foo Co (2)/Client.md")
    second = make(lib, "client", "Foo Co", allow_similar=True)
    assert (second.id, second.path) == ("foo-co-2", "Foo Co (3)/Client.md")


def test_id_suffix_and_filename_suffix_advance_separately(lib, library_root):
    write(library_root, "Taken/Client.md", record_bytes("client", "foo-co", "Taken"))
    result = make(lib, "client", "Foo Co")
    assert (result.id, result.path) == ("foo-co-2", "Foo Co/Client.md")


def test_client_directory_collision_is_casefolded(lib, library_root):
    (library_root / "FOO CO").mkdir()
    assert make(lib, "client", "foo co").path == "foo co (2)/Client.md"


def test_a_new_client_never_reuses_an_existing_directory(lib, library_root):
    write(library_root, "Foo Co/notes.txt", "keep me")
    result = make(lib, "client", "Foo Co")
    assert result.path == "Foo Co (2)/Client.md"
    assert (library_root / "Foo Co/notes.txt").read_text() == "keep me"


def test_project_filename_collisions(lib, library_root):
    write(library_root, "Acme Bakery/Projects/Menu Refresh.md", "# plain note, not a record\n")
    first = make(lib, "project", "Menu Refresh", client="acme-bakery")
    assert first.path == "Acme Bakery/Projects/Menu Refresh (2).md"
    second = make(lib, "project", "menu refresh", client="acme-bakery", allow_similar=True)
    assert second.path == "Acme Bakery/Projects/menu refresh (3).md"
    assert (library_root / "Acme Bakery/Projects/Menu Refresh.md").read_text() == "# plain note, not a record\n"


def test_a_directory_named_like_the_project_does_not_collide_with_the_md_file(lib, library_root):
    (library_root / "Acme Bakery/Projects/Menu Refresh").mkdir()
    assert make(lib, "project", "Menu Refresh", client="acme-bakery").path == "Acme Bakery/Projects/Menu Refresh.md"


def test_unsafe_characters_are_replaced_in_filenames(lib):
    assert make(lib, "client", 'A/B\\C:D*E?"F<G>H|I').path == "A B C D E F G H I/Client.md"


def test_windows_reserved_device_names_get_an_underscore(lib):
    assert make(lib, "client", "CON").path == "CON_/Client.md"
    assert make(lib, "project", "aux.notes", client="acme-bakery").path == "Acme Bakery/Projects/aux_.notes.md"


def test_dot_only_names_fall_back_to_the_id(lib):
    assert make(lib, "client", "..").path == "client/Client.md"


def test_filename_stem_is_capped(lib):
    path = make(lib, "client", "N" * 190).path
    assert path.split("/")[0] == "N" * 80


def test_pre_existing_file_at_the_destination_is_never_overwritten(lib, library_root, monkeypatch):
    sentinel = b"SENTINEL - not a record\n"
    write(library_root, "Acme Bakery/Projects/Menu Refresh.md", sentinel)
    real = library_module._listdir
    # The collision check cannot see the file (another process created it a moment ago),
    # so only the exclusive create stands between the sentinel and an overwrite.
    monkeypatch.setattr(library_module, "_listdir", lambda p: [n for n in real(p) if n != "Menu Refresh.md"])
    result = make(lib, "project", "Menu Refresh", client="acme-bakery")
    assert result.path == "Acme Bakery/Projects/Menu Refresh (2).md"
    assert (library_root / "Acme Bakery/Projects/Menu Refresh.md").read_bytes() == sentinel


def test_pre_existing_client_directory_race_moves_to_the_next_suffix(lib, library_root, monkeypatch):
    (library_root / "Foo Co").mkdir()
    real = library_module._listdir
    monkeypatch.setattr(library_module, "_listdir", lambda p: [n for n in real(p) if n != "Foo Co"])
    assert make(lib, "client", "Foo Co").path == "Foo Co (2)/Client.md"


def test_link_unsupported_falls_back_to_exclusive_create(lib, library_root, monkeypatch):
    fired = []

    def nope(src, dst, **kw):
        fired.append((src, dst))
        raise OSError(errno.EPERM, "links unsupported")

    patch_link(monkeypatch, nope)
    result = make(lib, "client", "Foo Co")
    assert fired, "the platform's hard-link call was not exercised"
    assert rev((library_root / result.path).read_bytes()) == result.rev
    assert [p for p in library_root.rglob("*") if p.name.startswith(".engagements-")] == []


def test_fallback_still_never_overwrites(lib, library_root, monkeypatch):
    sentinel = b"SENTINEL\n"
    write(library_root, "Acme Bakery/Projects/Menu Refresh.md", sentinel)
    real = library_module._listdir
    monkeypatch.setattr(library_module, "_listdir", lambda p: [n for n in real(p) if n != "Menu Refresh.md"])
    fired = []

    def nope(src, dst, **kw):
        fired.append((src, dst))
        raise OSError(errno.EPERM, "links unsupported")

    patch_link(monkeypatch, nope)
    assert make(lib, "project", "Menu Refresh", client="acme-bakery").path.endswith("Menu Refresh (2).md")
    assert fired, "the platform's hard-link call was not exercised"
    assert (library_root / "Acme Bakery/Projects/Menu Refresh.md").read_bytes() == sentinel


# --- size limits -------------------------------------------------------------


def test_result_over_one_mebibyte_is_too_large_and_nothing_is_written(lib, library_root):
    sections = [{"section": f"Section {i}", "body": "\U0001F600" * 20000} for i in range(30)]  # about 2.4 MB
    before = tree(library_root)
    err = fails(lib, "invalid_op", sections=sections)
    assert err.details["reason"] == "too_large"
    assert tree(library_root) == before


def test_result_just_under_one_mebibyte_is_accepted(lib, library_root):
    # 1 MiB minus headroom for the template: 13 sections of 80,000 bytes would exceed the schema,
    # so fill 30 sections with ASCII to land close to, not over, the limit.
    sections = [{"section": f"Section {i}", "body": "x" * 20000} for i in range(30)]
    result = make(lib, "client", "Foo Co", sections=sections)
    assert len((library_root / result.path).read_bytes()) < 1024 * 1024


def test_frontmatter_closing_line_past_65536_bytes_is_too_large(lib, library_root):
    locations = ["\U0001F600" * 1024 for _ in range(50)]  # about 200 KB of frontmatter
    before = tree(library_root)
    err = fails(lib, "invalid_op", kind="project", name="Menu Refresh", client="acme-bakery",
                properties={"locations": locations})
    assert err.details["reason"] == "too_large"
    assert tree(library_root) == before


def test_frontmatter_that_fits_exactly_within_the_window_is_accepted(lib, library_root):
    locations = ["l" * 1024 for _ in range(50)]  # about 51 KB, inside the window
    result = make(lib, "project", "Menu Refresh", client="acme-bakery", properties={"locations": locations})
    record = parse((library_root / result.path).read_bytes())
    assert record.frontmatter_span[1] <= 65536 and record.properties["locations"] == locations


# --- error mapping and metadata ----------------------------------------------


def test_filesystem_failure_maps_to_io_error_and_cleans_up(lib, library_root, monkeypatch):
    def refuse(src, dst, **kw):
        raise OSError(errno.EACCES, "denied: /secret/place")

    patch_link(monkeypatch, refuse)
    before = tree(library_root)
    err = fails(lib, "io_error", name="Foo Co")
    assert "secret/place" not in err.message and "Traceback" not in err.message
    assert tree(library_root) == before


def test_write_failure_midway_leaves_nothing_behind(lib, library_root, monkeypatch):
    real_write = os.write

    def broken(fd, data):
        raise OSError(errno.ENOSPC, "no space")

    monkeypatch.setattr(os, "write", broken)
    before = tree(library_root)
    fails(lib, "io_error", name="Foo Co")
    monkeypatch.setattr(os, "write", real_write)
    assert tree(library_root) == before


def test_symlinked_locks_directory_is_io_error(lib, library_root, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (library_root / ".engagements/locks").symlink_to(elsewhere)
    fails(lib, "io_error", name="Foo Co")
    assert list(elsewhere.iterdir()) == []


def test_missing_locks_directory_is_recreated(lib, library_root):
    assert not (library_root / ".engagements/locks").exists()
    make(lib, "client", "Foo Co")
    assert (library_root / ".engagements/locks/_library.lock").exists()


def test_destination_outside_the_root_is_refused(lib, library_root, monkeypatch):
    discovery = lib.discover()
    entry = next(e for e in discovery.entries if e.id == "acme-bakery")
    from dataclasses import replace

    bad = replace(entry, relpath="../escape/Client.md")
    discovery.entries[discovery.entries.index(entry)] = bad
    monkeypatch.setattr(lib, "discover", lambda fresh=False: discovery, raising=False)
    fails(lib, "outside_root", kind="project", name="Menu Refresh", client="acme-bakery")


def test_first_create_in_a_new_default_root_reports_library_created(tmp_path):
    from engagements.library import open_library

    lib = open_library(tmp_path / "fresh", create_default=True)
    result = make(lib, "client", "Foo Co")
    assert [w["code"] for w in result.warnings] == ["library_created"]
    assert result.path == "Foo Co/Client.md"
    again = make(open_library(tmp_path / "fresh", create_default=True), "client", "Bar Co")
    assert again.warnings == []


def test_lock_timeout_when_the_library_lock_is_held(lib, library_root, monkeypatch):
    monkeypatch.setattr(library_module, "LOCK_TIMEOUT", 0.1)
    lock = library_root / ".engagements" / "locks" / "_library.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(lock):
        fails(lib, "lock_timeout", name="Foo Co")
    assert make(lib, "client", "Foo Co").id == "foo-co"


def test_created_record_is_found_by_find_and_resolve(lib):
    made = make(lib, "client", "Foo Co", properties={"aliases": ["Fooey"]})
    assert lib.resolve("fooey").id == made.id
    assert [r["id"] for r in lib.find("foo").results] == ["foo-co"]
