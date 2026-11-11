"""A frontmatter field of the wrong YAML type is a defect, not a valid record.

``name: 123`` or a project's ``client: true`` used to parse with no defect at all:
doctor reported the library clean and find dropped the record without naming it.
A wrongly typed identity field is now a global defect, and a wrongly typed
optional field a targeted one.
"""

from pathlib import Path

import pytest
from conftest import write
from engagements import records
from engagements.doctor import run_doctor
from engagements.errors import EngagementsError
from engagements.library import open_library
from writer_support import ACME, do_update, make_library, op_status

LIBRARY_YAML = "format: 1\nlibrary_id: 33333333-3333-4333-8333-333333333333\n"
HOME = "/home/tester"
DOC = Path(__file__).resolve().parent.parent / "docs" / "TOOLS.md"


def parse_frontmatter(kind, frontmatter):
    return records.parse(b"---\nengagements: " + kind.encode() + b"\n" + frontmatter +
                         b"---\n\n## Summary\n")


def doctor(root):
    """The doctor with a deterministic, empty host filesystem."""
    return run_doctor(root, home=HOME, os_name="posix", exists=lambda path: False, realpath=lambda path: path)


def findings(report, code):
    return [finding for finding in report.findings if finding.code == code]


# --- the parser ----------------------------------------------------------------


@pytest.mark.parametrize("value", [b"123", b"true", b"[Acme Bakery]"])
def test_a_client_name_that_is_not_a_string_is_a_global_defect(value):
    record = parse_frontmatter("client", b"id: acme-bakery\nname: " + value + b"\n")

    assert record.kind == "client"
    assert record.defects == {"invalid_type"}
    assert record.mistyped_keys == {"name"}
    assert record.properties["name"] is None
    assert record.name is None
    assert record.property_defects == {}


@pytest.mark.parametrize("value", [b"123", b"true", b"[acme-bakery]"])
def test_a_project_client_that_is_not_a_string_is_a_global_defect(value):
    record = parse_frontmatter("project", b"id: acme-web\nname: Website\nclient: " + value + b"\n")

    assert record.kind == "project"
    assert record.defects == {"invalid_type"}
    assert record.mistyped_keys == {"client"}
    assert record.properties["client"] is None


def test_a_nested_mapping_identity_value_keeps_its_unsupported_defect():
    # A nested mapping already reads as ``{"unsupported_yaml": "dict"}``, which is a
    # global defect of its own; it is not reported twice under another name.
    record = parse_frontmatter("project", b"id: acme-web\nname: Website\nclient: {id: acme-bakery}\n")

    assert record.defects == {"unsupported_yaml"}
    assert record.mistyped_keys == set()


def test_a_date_or_datetime_identity_value_arrives_as_its_iso_string():
    # TOOLS.md "JSON property values": dates become ISO 8601 strings, so what the
    # parser reads is a string and the field is not mistyped.
    record = parse_frontmatter("client", b"id: acme-bakery\nname: 2026-01-01\n")

    assert record.defects == set()
    assert record.name == "2026-01-01"


def test_a_valid_identity_stays_clean():
    client = parse_frontmatter("client", b"id: acme-bakery\nname: Acme Bakery\n")
    project = parse_frontmatter("project", b"id: acme-web\nname: Website\nclient: acme-bakery\n")

    assert client.defects == set() and client.mistyped_keys == set()
    assert project.defects == set() and project.mistyped_keys == set()


def test_a_null_identity_field_is_reported_as_missing_not_mistyped():
    record = parse_frontmatter("client", b"id: acme-bakery\nname: null\n")

    assert record.defects == {"missing_field"}


def test_a_non_string_id_keeps_its_own_defect():
    record = parse_frontmatter("client", b"id: 123\nname: Acme Bakery\n")

    assert record.defects == {"invalid_id"}


def test_client_is_identity_only_on_a_project():
    # TOOLS.md: an extra client property on a client stays in the record; it is not
    # a relationship, so a value of another type there is not an identity defect.
    record = parse_frontmatter("client", b"id: acme-bakery\nname: Acme Bakery\nclient: true\n")

    assert record.defects == set()
    assert record.properties["client"] is True


@pytest.mark.parametrize(("frontmatter", "key"), [
    (b"id: acme-bakery\nname: Acme Bakery\nstatus: 5\n", "status"),
    (b"id: acme-bakery\nname: Acme Bakery\nstatus: true\n", "status"),
    (b"id: acme-bakery\nname: Acme Bakery\naliases: Acme\n", "aliases"),
    (b"id: acme-bakery\nname: Acme Bakery\naliases: 5\n", "aliases"),
])
def test_an_optional_field_of_the_wrong_type_is_a_targeted_defect(frontmatter, key):
    record = parse_frontmatter("client", frontmatter)

    assert record.defects == set()
    assert record.property_defects == {key: "invalid_type"}


def test_a_list_holding_a_non_string_stays_an_unsupported_value():
    record = parse_frontmatter("client", b"id: acme-bakery\nname: Acme Bakery\naliases: [1, 2]\n")

    assert record.property_defects == {"aliases": "unsupported_yaml"}


def test_a_marker_that_is_not_client_or_project_still_does_not_make_a_record():
    # Recognition is the plain marker value (TOOLS.md "Recognition (C2)"): the
    # parser does not guess a kind from a value of another type.
    record = parse_frontmatter("client", b"id: acme-bakery\nname: Acme Bakery\n")
    assert record.is_record
    assert records.parse(b"---\nengagements: true\nid: acme-bakery\nname: Acme Bakery\n---\n").is_record is False


# --- doctor ---------------------------------------------------------------------


def test_doctor_reports_a_mistyped_name_as_an_error_on_its_own_line(tmp_path):
    root = tmp_path / "library"
    write(root, ".engagements/library.yaml", LIBRARY_YAML)
    write(root, "Bad/Client.md",
          b"---\nengagements: client\nid: bad-name\nname: 123\n---\n\n## Summary\n")

    report = doctor(root)

    (finding,) = findings(report, "invalid_type")
    assert finding.severity == "error"
    assert (finding.path, finding.key, finding.line) == ("Bad/Client.md", "name", 4)
    assert not report.clean


def test_doctor_reports_a_mistyped_project_client_as_an_error(tmp_path):
    root = tmp_path / "library"
    write(root, ".engagements/library.yaml", LIBRARY_YAML)
    write(root, "Acme Bakery/Client.md",
          b"---\nengagements: client\nid: acme-bakery\nname: Acme Bakery\n---\n\n## Summary\n")
    write(root, "Acme Bakery/Projects/Website.md",
          b"---\nengagements: project\nid: acme-web\nname: Website\nclient: true\n---\n\n## Purpose\n")

    report = doctor(root)

    (finding,) = findings(report, "invalid_type")
    assert finding.severity == "error"
    assert (finding.path, finding.key) == ("Acme Bakery/Projects/Website.md", "client")


@pytest.mark.parametrize(("frontmatter", "key"), [
    (b"id: acme-bakery\nname: Acme Bakery\nstatus: 5\n", "status"),
    (b"id: acme-bakery\nname: Acme Bakery\naliases: Acme\n", "aliases"),
])
def test_doctor_warns_about_an_optional_field_of_the_wrong_type(tmp_path, frontmatter, key):
    root = tmp_path / "library"
    write(root, ".engagements/library.yaml", LIBRARY_YAML)
    write(root, "Bad/Client.md", b"---\nengagements: client\n" + frontmatter + b"---\n\n## Summary\n")

    report = doctor(root)

    (finding,) = findings(report, "invalid_type")
    assert finding.severity == "warning"
    assert finding.key == key


# --- find -----------------------------------------------------------------------


@pytest.fixture
def lib(opened_lib):
    return opened_lib


def test_find_lists_a_project_whose_client_is_not_a_string(lib, library_root):
    write(library_root, "Extra/Mistyped Client.md",
          b"---\nengagements: project\nid: mistyped-client\nname: Mistyped Client\nclient: true\n"
          b"---\n\n## Purpose\n")

    result = lib.find("mistyped-client")

    (row,) = result.results
    assert row["id"] == "mistyped-client"
    assert row["kind"] == "project"
    assert row["valid"] is False
    assert row["client"] is None
    assert result.malformed_skipped == 0


def test_find_lists_a_client_whose_name_is_not_a_string(lib, library_root):
    write(library_root, "Extra/Mistyped Name.md",
          b"---\nengagements: client\nid: mistyped-name\nname: 123\n---\n\n## Summary\n")

    result = lib.find("mistyped")

    (row,) = result.results
    assert row["id"] == "mistyped-name"
    assert row["name"] is None
    assert row["valid"] is False
    assert row["match"] == {"field": "id", "value": "mistyped-name"}
    assert result.malformed_skipped == 0


@pytest.mark.parametrize(("kind", "frontmatter"), [
    ("client", b""),
    ("client", b"status: true\n"),
    ("client", b"aliases: true\n"),
    ("client", b"name: null\nstatus: 5\n"),
    ("client", b"name: ~\naliases: [1]\n"),
    ("client", b"name:\nstatus: 5\n"),
    ("project", b"client: true\n"),
    ("project", b"client: acme-bakery\nname: null\nstatus: true\n"),
])
def test_find_still_counts_a_record_with_no_name_at_all(lib, library_root, kind, frontmatter):
    # The listing below is for a name that is present but unreadable. A record with no
    # usable name has nothing to identify it by, so it stays counted only, whatever
    # defect another field contributes: a mistyped status, aliases or project client
    # used to make every such record listable.
    write(library_root, f"Extra/No Name {kind}.md",
          b"---\nengagements: " + kind.encode() + b"\nid: no-name-co\n" + frontmatter
          + b"---\n\n## Summary\n")

    result = lib.find("no-name")

    assert result.results == []
    assert result.malformed_skipped == 1


def test_a_nameless_record_with_a_defect_never_displaces_a_valid_client(lib, library_root):
    # The public first page of an empty query, which is exactly where a record listed
    # for the wrong reason used to push a valid client out of `limit: 1`.
    write(library_root, "Extra/No Name.md",
          b"---\nengagements: client\nid: no-name-co\nstatus: true\n---\n\n## Summary\n")

    result = lib.find("", limit=1)

    (row,) = result.results
    assert row["id"] == "acme-bakery" and row["valid"] is True
    assert result.total == 5
    assert result.truncated is True
    assert result.malformed_skipped == 1


@pytest.mark.parametrize(("kind", "value"), [
    ("client", b"{id: acme-bakery}"),
    ("client", b"[123]"),
    ("client", b"[acme, 1]"),
    ("client", b".nan"),
    ("project", b"{id: acme-bakery}"),
    ("project", b"[123]"),
])
def test_find_lists_a_name_of_any_wrong_yaml_type(lib, library_root, kind, value):
    # A mapping, a list of non-strings, a mixed list or a non-finite float is an
    # unsupported value, so the record keeps its `unsupported_yaml` defect: the listing
    # decision is about the name, not about which defect code the value produced.
    extra = b"client: acme-bakery\n" if kind == "project" else b""
    write(library_root, f"Extra/Unreadable {kind}.md",
          b"---\nengagements: " + kind.encode() + b"\nid: unreadable-name\n" + extra
          + b"name: " + value + b"\n---\n\n## Summary\n")

    result = lib.find("unreadable-name")

    (row,) = result.results
    assert (row["id"], row["kind"], row["name"], row["valid"]) == ("unreadable-name", kind, None, False)
    assert row["match"] == {"field": "id", "value": "unreadable-name"}
    assert result.malformed_skipped == 0


@pytest.mark.parametrize("frontmatter", [
    b'id: unwritable\n"name": Acme Bakery\n',
    b"id: unwritable\nname: |\n  Acme Bakery\n",
    b"id: unwritable\nname: !!str 123\n",
])
def test_a_name_that_is_a_string_but_unwritable_stays_counted(lib, library_root, frontmatter):
    # The listing exception is about the value's type. A name written in a form that
    # cannot be rewritten has a string value, so there is no readable name to list by:
    # it stays counted, exactly as it was before.
    write(library_root, "Extra/Unwritable Name.md",
          b"---\nengagements: client\n" + frontmatter + b"---\n\n## Summary\n")

    result = lib.find("unwritable")

    assert result.results == []
    assert result.malformed_skipped == 1


def test_an_empty_query_lists_an_unsupported_name_as_invalid(lib, library_root):
    write(library_root, "Extra/Unreadable.md",
          b"---\nengagements: client\nid: unreadable-name\nname: {id: acme-bakery}\n"
          b"---\n\n## Summary\n")

    result = lib.find("")

    row = next(item for item in result.results if item["id"] == "unreadable-name")
    assert row["valid"] is False and row["match"] is None
    assert result.total == 6


# --- an explicit null on an optional field ---------------------------------------


@pytest.mark.parametrize("null", [b"null", b"~", b""])
def test_an_explicit_null_on_an_optional_field_is_not_a_type_defect(null):
    # TOOLS.md "Targeted defects": null is not a value of the wrong type, so it is not
    # `invalid_type`; the property reads as null and the record stays clean.
    record = parse_frontmatter("client", b"id: acme-bakery\nname: Acme Bakery\nstatus: " + null + b"\n")

    assert record.defects == set()
    assert record.property_defects == {}
    assert record.properties["status"] is None


@pytest.mark.parametrize("key", ["status", "aliases"])
def test_a_null_optional_field_stays_valid_and_doctor_clean(tmp_path, key):
    root = tmp_path / "library"
    write(root, ".engagements/library.yaml", LIBRARY_YAML)
    write(root, "Acme Bakery/Client.md",
          b"---\nengagements: client\nid: acme-bakery\nname: Acme Bakery\n" + key.encode()
          + b": null\n---\n\n## Summary\n")

    report = doctor(root)

    assert report.clean and report.findings == []
    (row,) = open_library(root, create_default=False).find("acme-bakery").results
    assert row["valid"] is True
    if key == "status":
        # An explicit null is not the absent-property `active` default either.
        assert row["status"] is None


def test_the_optional_type_rule_documents_the_null_exception():
    bullet = next(line for line in DOC.read_text(encoding="utf-8").splitlines()
                  if line.startswith("- `invalid_type` on an optional property"))
    assert "An explicit `null`" in bullet
    assert "defect-free" in bullet


def test_an_empty_query_lists_the_mistyped_record_as_invalid(lib, library_root):
    write(library_root, "Extra/Mistyped Name.md",
          b"---\nengagements: client\nid: mistyped-name\nname: 123\n---\n\n## Summary\n")

    result = lib.find("")

    row = next(item for item in result.results if item["id"] == "mistyped-name")
    assert row["valid"] is False and row["match"] is None
    assert result.total == 6


def test_an_update_is_refused_the_way_any_other_global_defect_is(tmp_path):
    lib = make_library(tmp_path, ACME.replace("name: Acme Bakery\n", "name: 123\n"))

    with pytest.raises(EngagementsError) as excinfo:
        do_update(lib, "0" * 16, [op_status()])
    assert excinfo.value.code == "malformed"
    assert {defect["kind"] for defect in excinfo.value.details["defects"]} == {"invalid_type"}
