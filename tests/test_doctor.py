"""Doctor checks (spec 6.6): read-only diagnostics over a library root.

Fictional data only. Secrets below are fake values assembled at runtime so no
real-looking token ever sits in the source, and the tests prove the report never
echoes them.
"""

import json
import os
import shutil

import pytest
from conftest import FIXTURE_LIBRARY, record_bytes, write
from engagements.doctor import DoctorReport, Finding, run_doctor

HOME = "/home/tester"
LIBRARY_YAML = "format: 1\nlibrary_id: 22222222-2222-4222-8222-222222222222\n"


# --- helpers -------------------------------------------------------------------


def doctor(root, **kwargs):
    """Run the doctor with a deterministic, empty host filesystem unless overridden."""
    kwargs.setdefault("home", HOME)
    kwargs.setdefault("os_name", "posix")
    kwargs.setdefault("exists", lambda path: False)
    kwargs.setdefault("realpath", lambda path: path)
    return run_doctor(root, **kwargs)


def findings(report, code):
    return [finding for finding in report.findings if finding.code == code]


def make_library(root):
    """A small, well-formed library: one client and one project."""
    write(root, ".engagements/library.yaml", LIBRARY_YAML)
    write(root, "Acme Bakery/Client.md", record_bytes("client", "acme-bakery", "Acme Bakery"))
    write(root, "Acme Bakery/Projects/Website 2026.md",
          record_bytes("project", "acme-website-2026", "Website 2026", client="acme-bakery"))
    return root


def add_client(root, client_id="acme-bakery", name="Acme Bakery"):
    write(root, f"{name}/Client.md", record_bytes("client", client_id, name))


def project_bytes(project_id, client, name, locations, status="active"):
    lines = ["---", "engagements: project", f"id: {project_id}", f"client: {client}",
             f"name: {name}", f"status: {status}",
             "locations: [" + ", ".join(locations) + "]", "updated: 2026-11-05", "---",
             "", "## Purpose", ""]
    return "\n".join(lines).encode()


# --- library root and metadata ------------------------------------------------


def test_library_missing_root_is_an_error_and_creates_nothing(tmp_path):
    root = tmp_path / "absent"
    report = doctor(root)

    assert report.clean is False
    (finding,) = findings(report, "library_missing")
    assert finding.severity == "error" and finding.path is None
    assert report.checked == 0
    assert not root.exists()


def test_library_missing_when_root_is_a_file(tmp_path):
    path = tmp_path / "library.txt"
    path.write_text("not a library")

    report = doctor(path)

    assert findings(report, "library_missing")
    assert report.checked == 0


def test_missing_library_yaml_is_an_error_but_discovery_still_runs(tmp_path):
    root = tmp_path / "library"
    shutil.copytree(FIXTURE_LIBRARY, root)
    (root / ".engagements" / "library.yaml").unlink()

    report = doctor(root)

    (finding,) = findings(report, "library_yaml")
    assert finding.severity == "error"
    assert finding.path == ".engagements/library.yaml"
    assert "missing" in finding.message
    assert report.checked == 5


def test_invalid_library_yaml_is_an_error(tmp_path):
    root = tmp_path / "library"
    shutil.copytree(FIXTURE_LIBRARY, root)
    write(root, ".engagements/library.yaml", "format: 2\nlibrary_id: nope\n")

    report = doctor(root)

    (finding,) = findings(report, "library_yaml")
    assert "invalid" in finding.message or "format" in finding.message
    assert report.checked == 5


def test_doctor_never_creates_metadata_or_locks(tmp_path):
    root = tmp_path / "library"
    shutil.copytree(FIXTURE_LIBRARY, root)
    shutil.rmtree(root / ".engagements")
    before = sorted((p.relative_to(root).as_posix(), p.is_dir()) for p in root.rglob("*"))

    report = doctor(root)

    after = sorted((p.relative_to(root).as_posix(), p.is_dir()) for p in root.rglob("*"))
    assert before == after
    assert not (root / ".engagements").exists()
    assert findings(report, "library_yaml")
    assert report.checked == 5


def test_invalid_library_yaml_does_not_take_or_create_a_lock(tmp_path):
    root = tmp_path / "library"
    shutil.copytree(FIXTURE_LIBRARY, root)
    write(root, ".engagements/library.yaml", "format: 1\n")
    (root / ".engagements" / "locks").mkdir()

    report = doctor(root)

    assert findings(report, "library_yaml")
    assert list((root / ".engagements" / "locks").iterdir()) == []
    assert report.checked == 5


# --- filesystem errors ---------------------------------------------------------


@pytest.mark.skipif(os.name == "nt" or getattr(os, "geteuid", lambda: 1)() == 0,
                    reason="permission bits are not enforced for this user")
def test_io_error_for_an_unreadable_record(tmp_path):
    root = make_library(tmp_path / "library")
    path = write(root, "Extra/Notes.md", record_bytes("client", "other-client", "Other"))
    os.chmod(path, 0)
    try:
        report = doctor(root)
    finally:
        os.chmod(path, 0o600)

    (finding,) = findings(report, "io_error")
    assert finding.severity == "error"
    assert finding.path == "Extra/Notes.md"


@pytest.mark.skipif(os.name == "nt" or getattr(os, "geteuid", lambda: 1)() == 0,
                    reason="permission bits are not enforced for this user")
def test_io_error_for_an_unreadable_subtree(tmp_path):
    root = make_library(tmp_path / "library")
    write(root, "Locked/Notes.md", record_bytes("client", "other-client", "Other"))
    directory = root / "Locked"
    os.chmod(directory, 0)
    try:
        report = doctor(root)
    finally:
        os.chmod(directory, 0o700)

    (finding,) = findings(report, "io_error")
    assert finding.path == "Locked"


def test_read_failure_after_discovery_is_a_finding(tmp_path, monkeypatch):
    root = make_library(tmp_path / "library")

    def unreadable(library, relpath):
        raise OSError("this must never escape")

    monkeypatch.setattr("engagements.doctor._read_bytes", unreadable)

    report = doctor(root)

    assert {f.path for f in findings(report, "io_error")} == {
        "Acme Bakery/Client.md", "Acme Bakery/Projects/Website 2026.md"}
    assert report.checked == 2


# --- record defects ------------------------------------------------------------


def test_yaml_parse_error_names_the_line_but_not_the_text(tmp_path):
    root = make_library(tmp_path / "library")
    write(root, "Bad/Broken.md",
          b"---\nengagements: client\nid: broken\nname: [SECRETYAMLLINE\n---\n")

    report = doctor(root)

    (finding,) = findings(report, "yaml_parse")
    assert finding.severity == "error" and finding.path == "Bad/Broken.md"
    assert finding.line == 1
    assert "SECRETYAMLLINE" not in report.render_text()
    assert "SECRETYAMLLINE" not in json.dumps(report.as_dict())


def test_missing_field_reports_the_field(tmp_path):
    root = make_library(tmp_path / "library")
    write(root, "Bad/NoName.md", b"---\nengagements: client\nid: no-name\n---\n\n## Summary\n")
    write(root, "Bad/NoClient.md",
          b"---\nengagements: project\nid: no-client\nname: No Client\n---\n\n## Purpose\n")

    report = doctor(root)

    missing = {(f.path, f.key) for f in findings(report, "missing_field")}
    assert ("Bad/NoName.md", "name") in missing
    assert ("Bad/NoClient.md", "client") in missing
    assert all(f.severity == "error" for f in findings(report, "missing_field"))


def test_invalid_id_is_an_error_with_the_property_line(tmp_path):
    root = make_library(tmp_path / "library")
    write(root, "Bad/BadId.md",
          b"---\nengagements: client\nid: Bad_ID\nname: Bad Id\n---\n\n## Summary\n")

    report = doctor(root)

    (finding,) = findings(report, "invalid_id")
    assert finding.severity == "error" and finding.key == "id" and finding.line == 3


def test_duplicate_id_reports_every_record_and_names_the_others(tmp_path):
    root = make_library(tmp_path / "library")
    write(root, "Extra/First.md", record_bytes("client", "twin", "First"))
    write(root, "Extra/Second.md", record_bytes("client", "twin", "Second"))

    report = doctor(root)

    duplicates = findings(report, "duplicate_id")
    assert {f.path for f in duplicates} == {"Extra/First.md", "Extra/Second.md"}
    assert all(f.severity == "error" for f in duplicates)
    assert "Extra/Second.md" in next(f.message for f in duplicates if f.path == "Extra/First.md")
    assert "Extra/First.md" in next(f.message for f in duplicates if f.path == "Extra/Second.md")


def test_unknown_client_is_an_error_on_the_project(tmp_path):
    root = make_library(tmp_path / "library")
    write(root, "Extra/Orphan.md",
          record_bytes("project", "orphan", "Orphan", client="ghost-client"))

    report = doctor(root)

    (finding,) = findings(report, "unknown_client")
    assert finding.severity == "error" and finding.path == "Extra/Orphan.md"
    assert finding.key == "client"


def test_encoding_error_does_not_echo_raw_bytes(tmp_path):
    root = make_library(tmp_path / "library")
    write(root, "Bad/Latin.md",
          b"---\nengagements: client\nid: latin\nname: Latin\n---\n\n## Summary\nRAWBYTESMARKER\xff\n")

    report = doctor(root)

    (finding,) = findings(report, "encoding")
    assert finding.severity == "error" and finding.path == "Bad/Latin.md"
    assert "RAWBYTESMARKER" not in report.render_text()
    assert "RAWBYTESMARKER" not in json.dumps(report.as_dict())


def test_mixed_eol_is_an_error(tmp_path):
    root = make_library(tmp_path / "library")
    write(root, "Bad/Mixed.md",
          b"---\r\nengagements: client\r\nid: mixed\r\nname: Mixed\r\n---\r\n\n## Summary\nx\n")

    report = doctor(root)

    (finding,) = findings(report, "mixed_eol")
    assert finding.severity == "error" and finding.path == "Bad/Mixed.md"


def test_kind_conflict_reports_a_null_kind(tmp_path):
    root = make_library(tmp_path / "library")
    write(root, "Bad/TwoKinds.md",
          b"---\nengagements: client\nengagements: project\nid: two-kinds\nname: Two Kinds\n---\n")

    report = doctor(root)

    (finding,) = findings(report, "kind_conflict")
    assert finding.severity == "error" and finding.path == "Bad/TwoKinds.md"
    # The repeated marker is also a global defect in its own right.
    assert any(f.code == "duplicate_key" and f.key == "engagements"
               for f in report.findings)


def test_duplicate_identity_key_is_an_error_with_the_first_line(tmp_path):
    root = make_library(tmp_path / "library")
    write(root, "Bad/DupId.md",
          b"---\nengagements: client\nid: first-id\nid: second-id\nname: Dup\n---\n")

    report = doctor(root)

    (finding,) = [f for f in findings(report, "duplicate_key") if f.key == "id"]
    assert finding.severity == "error" and finding.path == "Bad/DupId.md"
    assert finding.line == 3


def test_unsupported_identity_key_is_an_error(tmp_path):
    root = make_library(tmp_path / "library")
    write(root, "Bad/QuotedId.md",
          b"---\nengagements: client\n\"id\": acme-bakery\nname: Quoted\n---\n")

    report = doctor(root)

    (finding,) = findings(report, "unsupported_yaml")
    assert finding.severity == "error" and finding.key == "id" and finding.line == 3


def test_duplicate_non_identity_key_is_a_warning(tmp_path):
    root = make_library(tmp_path / "library")
    write(root, "Bad/DupAlias.md",
          b"---\nengagements: client\nid: dup-alias\nname: Dup\naliases: [One]\n"
          b"aliases: [Two]\n---\n")

    report = doctor(root)

    (finding,) = findings(report, "duplicate_key")
    assert finding.severity == "warning" and finding.key == "aliases" and finding.line == 5


def test_unsupported_non_identity_yaml_is_a_warning(tmp_path):
    root = make_library(tmp_path / "library")
    write(root, "Bad/Nested.md",
          b"---\nengagements: client\nid: nested\nname: Nested\ncustom: {nested: x}\n---\n")

    report = doctor(root)

    (finding,) = findings(report, "unsupported_yaml")
    assert finding.severity == "warning" and finding.key == "custom"


def test_client_key_is_identity_only_for_a_project(tmp_path):
    root = make_library(tmp_path / "library")
    write(root, "Bad/ClientDup.md",
          b"---\nengagements: client\nid: client-dup\nname: Client Dup\n"
          b"client: one\nclient: two\n---\n")
    write(root, "Bad/ProjectDup.md",
          b"---\nengagements: project\nid: project-dup\nclient: acme-bakery\n"
          b"client: other\nname: Project Dup\n---\n")

    report = doctor(root)

    client_key = [f for f in report.findings if f.key == "client" and f.code in
                  ("duplicate_key", "unsupported_yaml")]
    by_path = {f.path: f.severity for f in client_key}
    assert by_path == {"Bad/ClientDup.md": "warning", "Bad/ProjectDup.md": "error"}


def test_duplicate_section_warns_once_per_heading(tmp_path):
    root = make_library(tmp_path / "library")
    write(root, "Bad/TwoNotes.md",
          b"---\nengagements: client\nid: two-notes\nname: Two Notes\n---\n\n"
          b"## Notes\none\n\n## Notes\ntwo\n")

    report = doctor(root)

    duplicates = findings(report, "duplicate_section")
    assert [f.line for f in duplicates] == [7, 10]
    assert all(f.severity == "warning" and f.section == "notes" for f in duplicates)


# --- discovery warnings --------------------------------------------------------


def test_skipped_symlink_is_a_warning(tmp_path):
    root = make_library(tmp_path / "library")
    target = root / "Acme Bakery" / "Client.md"
    link = root / "Link.md"
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available")

    report = doctor(root)

    (finding,) = findings(report, "skipped_symlink")
    assert finding.severity == "warning" and finding.path == "Link.md"


def test_oversize_file_is_a_warning_without_a_size(tmp_path):
    root = make_library(tmp_path / "library")
    body = b"---\nengagements: client\nid: big\nname: Big\n---\n" + b"x" * (1024 * 1024)
    write(root, "Extra/Big.md", body)

    report = doctor(root)

    (finding,) = findings(report, "oversize")
    assert finding.severity == "warning" and finding.path == "Extra/Big.md"
    assert "1 MiB" in finding.message
    assert str(len(body)) not in finding.message


# --- secrets -------------------------------------------------------------------


def test_secret_shaped_strings_are_warnings_that_never_echo_the_value(tmp_path):
    key = "AKIA" + "ABCDEFGHIJKLMNOP"
    github = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4"
    slack = "xoxb-" + "1234567890-abcdefghij"
    password = "pa55word" + "value"
    pem = "-----BEGIN RSA " + "PRIVATE KEY-----"
    jwt = "eyJ" + "abcdefgh" + "." + "ijklmnop" + "." + "qrstuvwx"
    sk = "sk-" + "abcdefghijklmnopqrstuvwx"
    root = make_library(tmp_path / "library")
    write(root, "Secret/Keys.md", "\n".join([
        "---", "engagements: client", "id: secrets", "name: Secrets", "---", "",
        "## Systems of record",
        f"- Access key: {key}",
        f"- Token: {github}",
        f"- Slack: {slack}",
        f"- Password: {password}",
        pem,
        f"- JWT: {jwt}",
        f"- Key: {sk}",
        "",
    ]).encode())

    report = doctor(root)

    secrets = findings(report, "secret_shaped")
    # One line can hold more than one finding: an assignment whose value is also a
    # provider shape reports the assignment and the shape.
    assert {f.line for f in secrets} == {8, 9, 10, 11, 12, 13, 14}
    assert [f.line for f in secrets] == sorted(f.line for f in secrets)
    assert all(f.severity == "warning" for f in secrets)

    rendered = report.render_text()
    dumped = json.dumps(report.as_dict())
    for value in (key, github, slack, password, pem, jwt, sk):
        assert value not in rendered
        assert value not in dumped
    assert "value is not shown" in secrets[0].message


def test_placeholder_credentials_are_not_flagged(tmp_path):
    # SPEC-QUESTION: the placeholder allowlist is wider than the listed examples
    # (it also accepts "example", "see" and "from") to avoid documentation noise.
    # A run of masking characters is a placeholder; a hyphenated group is not a run.
    root = make_library(tmp_path / "library")
    write(root, "Secret/Placeholders.md", "\n".join([
        "---", "engagements: client", "id: placeholders", "name: Placeholders", "---", "",
        "## Systems of record",
        '- Credentials: password manager collection "Acme" (never copy values here)',
        "- token: <paste token here>",
        "- api_key: REDACTED",
        "- secret: xxxxxxxx",
        "- password: ***",
        "- Reference: see the password manager",
        "",
    ]).encode())

    report = doctor(root)

    assert findings(report, "secret_shaped") == []


# --- heading closing sequence --------------------------------------------------


def test_heading_with_unicode_space_before_closing_hashes_warns(tmp_path):
    root = make_library(tmp_path / "library")
    write(root, "Bad/Headings.md", (
        "---\nengagements: client\nid: headings\nname: Headings\n---\n\n"
        "```\n## Fenced ###\u00a0\n```\n\n"
        "## Notes ###\u00a0\nx\n\n"
        "## Summary ###\u2003\ny\n\n"
        "## Plain ###\nz\n"
    ).encode())

    report = doctor(root)

    flagged = findings(report, "heading_closing_sequence")
    assert {f.section for f in flagged} == {"notes ###", "summary ###"}
    assert [f.line for f in flagged] == [11, 14]
    assert all(f.severity == "warning" for f in flagged)
    assert "unicode whitespace" in flagged[0].message.lower()


# --- locations -----------------------------------------------------------------


def test_missing_location_ignores_urls_and_relative_paths(tmp_path):
    # SPEC-QUESTION: one finding per project and frontmatter line, because a flow
    # list puts every location on one line and the dedupe key has no location.
    root = tmp_path / "library"
    add_client(root)
    write(root, "Acme Bakery/Projects/Tilde.md", project_bytes(
        "acme-tilde", "acme-bakery", "Tilde", ["~/clients/missing", "https://drive.example/acme"]))
    write(root, "Acme Bakery/Projects/Absolute.md", project_bytes(
        "acme-absolute", "acme-bakery", "Absolute", ["/srv/example/missing"]))
    write(root, "Acme Bakery/Projects/Remote.md", project_bytes(
        "acme-remote", "acme-bakery", "Remote", ["https://drive.example/acme", "../relative"]))

    report = doctor(root)

    notices = findings(report, "missing_location")
    assert {f.path for f in notices} == {"Acme Bakery/Projects/Tilde.md",
                                         "Acme Bakery/Projects/Absolute.md"}
    assert all(f.severity == "notice" and f.key == "locations" for f in notices)


def test_missing_locations_cover_historical_projects_but_ambiguity_does_not(tmp_path):
    """SPEC 6.6: a missing location is reported for a historical project too.

    The shared/nested checks exist for auto-binding, which only consults active
    projects, so those stay active-only (contract decision).
    """
    root = tmp_path / "library"
    add_client(root)
    add_client(root, "riverbend-dental", "Riverbend Dental")
    write(root, "Acme Bakery/Projects/Shared.md",
          project_bytes("acme-shared", "acme-bakery", "Shared", ["~/clients/shared"]))
    write(root, "Riverbend Dental/Projects/Shared.md",
          project_bytes("riverbend-shared", "riverbend-dental", "Shared", ["~/clients/shared"]))
    write(root, "Riverbend Dental/Projects/Old.md", project_bytes(
        "riverbend-old", "riverbend-dental", "Old", ["~/clients/shared"], status="historical"))

    report = doctor(root)

    shared = findings(report, "shared_location")
    assert {f.path for f in shared} == {"Acme Bakery/Projects/Shared.md",
                                        "Riverbend Dental/Projects/Shared.md"}
    assert all(f.severity == "notice" for f in shared)
    acme = next(f for f in shared if f.path.startswith("Acme"))
    assert "riverbend-shared" in acme.message
    assert all("riverbend-old" not in f.message for f in shared)

    missing = findings(report, "missing_location")
    assert {f.path for f in missing} == {"Acme Bakery/Projects/Shared.md",
                                         "Riverbend Dental/Projects/Shared.md",
                                         "Riverbend Dental/Projects/Old.md"}
    assert all(f.severity == "notice" and f.key == "locations" for f in missing)


def test_nested_location_uses_component_boundaries(tmp_path):
    root = tmp_path / "library"
    add_client(root)
    add_client(root, "riverbend-dental", "Riverbend Dental")
    write(root, "Acme Bakery/Projects/Outer.md",
          project_bytes("acme-outer", "acme-bakery", "Outer", ["/w/outer"]))
    write(root, "Acme Bakery/Projects/Inner.md",
          project_bytes("acme-inner", "acme-bakery", "Inner", ["/w/outer/inner"]))
    write(root, "Riverbend Dental/Projects/Deep.md",
          project_bytes("riverbend-deep", "riverbend-dental", "Deep", ["/w/outer/deep"]))
    write(root, "Riverbend Dental/Projects/Sibling.md",
          project_bytes("riverbend-sibling", "riverbend-dental", "Sibling", ["/w/outermost"]))
    write(root, "Riverbend Dental/Projects/Both.md", project_bytes(
        "riverbend-both", "riverbend-dental", "Both", ["/w/tree", "/w/tree/sub"]))

    report = doctor(root, exists=lambda path: True, realpath=lambda path: path)

    nested = findings(report, "nested_location")
    assert {f.path for f in nested} == {"Acme Bakery/Projects/Inner.md",
                                        "Riverbend Dental/Projects/Deep.md"}
    assert all("acme-outer" in f.message for f in nested)


def test_messages_do_not_echo_an_invalid_project_id(tmp_path):
    root = tmp_path / "library"
    add_client(root)
    write(root, "A/One.md", project_bytes("/abs/path", "acme-bakery", "One", ["/w/shared"]))
    write(root, "A/Two.md", project_bytes("two", "acme-bakery", "Two", ["/w/shared"]))

    report = doctor(root, exists=lambda path: True, realpath=lambda path: path)

    shared = findings(report, "shared_location")
    assert len(shared) == 2
    assert all("/abs/path" not in f.message for f in shared)
    assert any("A/One.md" in f.message for f in shared)


# --- clean libraries -----------------------------------------------------------


def test_fixture_library_is_clean_with_only_notices():
    report = doctor(FIXTURE_LIBRARY)

    assert report.checked == 5
    assert report.clean is True
    assert report.counts()["error"] == 0
    assert report.counts()["warning"] == 0
    assert report.findings and all(f.severity == "notice" for f in report.findings)


def test_second_well_formed_library_is_clean(tmp_path):
    report = doctor(make_library(tmp_path / "library"))

    assert report.checked == 2
    assert report.clean is True
    assert report.findings == []


# --- report shape --------------------------------------------------------------


def test_report_sorts_findings_and_renders_one_line_each():
    error = Finding("error", "yaml_parse", "Broken frontmatter.", path="b.md", line=12)
    warning = Finding("warning", "duplicate_section", "Repeated heading.", path="a.md", line=3,
                      section="notes")
    notice = Finding("notice", "missing_location", "Missing location.", path="c.md", key="locations")
    report = DoctorReport(".", 3, [notice, warning, error])

    assert [f.severity for f in report.findings] == ["error", "warning", "notice"]
    assert report.clean is False
    assert report.counts() == {"error": 1, "warning": 1, "notice": 1}
    data = report.as_dict()
    assert data["clean"] is False and data["checked"] == 3
    assert data["findings"][0] == {"severity": "error", "code": "yaml_parse", "path": "b.md",
                                   "line": 12, "message": "Broken frontmatter."}

    text = report.render_text().splitlines()
    assert "Engagements doctor" in text[0]
    assert text[1] == "error   yaml_parse  b.md:12  Broken frontmatter."
    assert text[-1] == "Doctor: 1 error, 1 warning, 1 notice"

    same = Finding("notice", "missing_location", "Missing location.", path="c.md",
                   key="locations")
    assert len(DoctorReport(".", 1, [same, same]).findings) == 1


def test_as_dict_omits_absent_fields():
    assert Finding("error", "library_missing", "No root.").as_dict() == {
        "severity": "error", "code": "library_missing", "message": "No root."}


def test_outputs_never_contain_absolute_paths(tmp_path):
    root = tmp_path / "library"
    shutil.copytree(FIXTURE_LIBRARY, root)
    (root / ".engagements" / "library.yaml").unlink()

    report = doctor(root)

    rendered = report.render_text()
    dumped = json.dumps(report.as_dict())
    assert report.root_display == "."
    assert str(root) not in rendered and str(root) not in dumped
    assert str(tmp_path) not in rendered and str(tmp_path) not in dumped
    assert all(f.path is None or not os.path.isabs(f.path) for f in report.findings)
    assert "Doctor:" in rendered.splitlines()[-1]
