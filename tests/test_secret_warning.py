"""A write that looks like it holds a credential is saved with a stern warning.

The write is never refused for this reason: the record is saved, the result's
``warnings`` carries at most five ``possible_secret`` entries plus a count, and no
entry carries a value, a property name, a heading or any other text from the
record. A create judges the record it saved. An update compares the set of
findings before and after, so an unchanged secret does not warn again, a value
added and removed in one call does not warn, and a credential assembled across two
ops does. Doctor reports what is left in the record by area, section and line
until it is removed.
"""

import gc
import json
import time

import pytest
from engagements.doctor import run_doctor
from engagements.redact import API_KEY, ASSIGNMENT, KINDS
from engagements.tools import (
    POSSIBLE_SECRET_CODE,
    POSSIBLE_SECRET_MESSAGE,
    POSSIBLE_SECRET_OMITTED,
)
from tools_support import call, copy_library, make_session

FAKE = "Zq9" + "x7Lm2" + "Pw4Rt6Yb8Nc0"
SHORT = "Q7x9"
HEXLIKE = "ab12" * 6


def secret_warnings(result):
    return [warning for warning in result["warnings"] if warning["code"] == POSSIBLE_SECRET_CODE]


def doctor(root):
    """Doctor over ``root`` with a deterministic, empty host filesystem."""
    return run_doctor(root, home="/home/tester", os_name="posix",
                      exists=lambda path: False, realpath=lambda path: path)


def create_client(session, name, *, body="", section="Systems of record", properties=None, source=None,
                  kind="client", client=None):
    args = {"kind": kind, "name": name, "sections": [{"section": section, "body": body}]}
    if client is not None:
        args["client"] = client
    if properties is not None:
        args["properties"] = properties
    if source is not None:
        args["source"] = source
    return call("engagements_create", args, session)


def update(session, created, ops, source="chat"):
    return call("engagements_update", {"id": created["data"]["id"], "base_rev": created["data"]["rev"],
                                       "source": source, "ops": ops}, session)


def record_line(root, relpath, needle):
    """The 1-based line of the record that contains ``needle``."""
    for number, line in enumerate((root / relpath).read_text(encoding="utf-8").split("\n"), start=1):
        if needle in line:
            return number
    raise AssertionError(f"{needle!r} is not in {relpath}")


# --- create --------------------------------------------------------------------


def test_a_create_that_looks_like_a_credential_is_saved_and_warns(tmp_path):
    root = copy_library(tmp_path)
    session, _ = make_session(root)

    result = create_client(session, "Probe Bakery", body=f"CRM: Harbor\npassword: {FAKE}")

    assert result["ok"] is True, result
    record = root / result["data"]["path"]
    assert FAKE in record.read_text(encoding="utf-8")

    (warning,) = secret_warnings(result)
    assert warning["area"] == "section"
    assert warning["section"] == 6           # Systems of record
    assert warning["line"] == record_line(root, result["data"]["path"], FAKE)
    assert warning["kind"] == ASSIGNMENT
    assert FAKE not in json.dumps(result)


def test_each_finding_gets_its_own_warning_with_its_own_location(tmp_path):
    root = copy_library(tmp_path)
    session, _ = make_session(root)

    result = create_client(session, "Probe Dental",
                           properties={"aliases": ["Probe Dental", "sk_live_" + FAKE]},
                           body=f"email: hello@probe-dental.example\ntoken: {FAKE}")

    warnings = secret_warnings(result)
    assert [(warning["area"], warning["kind"]) for warning in warnings] == [
        ("property", "API key"), ("section", ASSIGNMENT)]
    assert warnings[0]["line"] == 5
    assert warnings[1]["line"] == record_line(root, result["data"]["path"], f"token: {FAKE}")


def test_a_credential_in_the_create_source_is_judged_in_the_change_log(tmp_path):
    root = copy_library(tmp_path)
    session, _ = make_session(root)

    result = create_client(session, "Probe Grocer", source=f"token: {FAKE}")

    assert result["ok"] is True, result
    (warning,) = secret_warnings(result)
    assert warning["area"] == "change_log"
    assert warning["section"] == 9
    assert warning["line"] == record_line(root, result["data"]["path"], FAKE)
    assert warning["kind"] == ASSIGNMENT
    assert FAKE not in json.dumps(result)


def test_a_credential_in_a_section_heading_is_a_section_finding(tmp_path):
    root = copy_library(tmp_path)
    session, _ = make_session(root)

    result = create_client(session, "Probe Baker", section=f"Bearer {FAKE}",
                           body=f"password: {HEXLIKE}")

    assert result["ok"] is True, result
    warnings = secret_warnings(result)
    assert [(warning["area"], warning["kind"]) for warning in warnings] == [
        ("section", "bearer token"), ("section", ASSIGNMENT)]
    assert all(isinstance(warning["section"], int) for warning in warnings)
    assert FAKE not in json.dumps(result)
    assert HEXLIKE not in json.dumps(result)


def test_a_credential_in_a_created_property_is_a_property_finding(tmp_path):
    """A create judges every location of the saved record, the frontmatter included."""
    root = copy_library(tmp_path)
    session, _ = make_session(root)

    result = create_client(session, "Probe Locations", kind="project", client="acme-bakery",
                           properties={"locations": [f"vault password: {SHORT}"]})

    assert result["ok"] is True, result
    warnings = secret_warnings(result)
    assert [(entry["area"], entry["kind"]) for entry in warnings] == [("property", ASSIGNMENT)]
    assert "section" not in warnings[0]
    assert warnings[0]["line"] == record_line(root, result["data"]["path"], SHORT)


def test_a_create_with_no_credential_shape_carries_no_such_warning(tmp_path):
    root = copy_library(tmp_path)
    session, _ = make_session(root)

    result = create_client(session, "Probe Hardware",
                           body="Reset her password on Monday.\nCRM: Harbor")

    assert result["ok"] is True, result
    assert secret_warnings(result) == []


def test_a_refused_create_carries_no_secret_warning(tmp_path):
    root = copy_library(tmp_path)
    session, _ = make_session(root)
    create_client(session, "Probe Optician")

    result = create_client(session, "Probe Optician", body=f"password: {FAKE}")

    assert result["ok"] is False
    assert result["error"]["code"] == "similar_exists"
    assert result["warnings"] == []


def test_warnings_are_bounded_and_the_rest_are_counted(tmp_path):
    root = copy_library(tmp_path)
    session, _ = make_session(root)
    body = "\n".join(f"password: {FAKE}{number}" for number in range(7))

    result = create_client(session, "Probe Print Shop", body=body)

    warnings = secret_warnings(result)
    lines = [warning["line"] for warning in warnings[:5]]
    assert lines == list(range(lines[0], lines[0] + 5))
    assert [warning["line"] for warning in warnings[1:5]] == [
        record_line(root, result["data"]["path"], f"password: {FAKE}{number}") for number in range(1, 5)]
    assert "line" not in warnings[-1]
    assert warnings[-1]["omitted"] == 2
    assert warnings[-1]["message"] == POSSIBLE_SECRET_OMITTED
    assert "omitted" not in warnings[0]
    assert FAKE not in json.dumps(result)


# --- update --------------------------------------------------------------------


def test_an_update_warns_for_a_credential_it_adds(tmp_path):
    root = copy_library(tmp_path)
    session, _ = make_session(root)
    created = create_client(session, "Probe Studio")

    result = update(session, created, [{"op": "append_to_section", "section": "Current work",
                                        "text": f"Client portal\nclient_secret = {FAKE}"}])

    assert result["ok"] is True, result
    (warning,) = secret_warnings(result)
    assert warning["area"] == "section"
    assert warning["section"] == 2
    assert warning["line"] == record_line(root, result["data"]["path"], FAKE)
    assert warning["kind"] == ASSIGNMENT
    assert FAKE not in json.dumps(result)


def test_an_update_warns_for_a_property_it_sets(tmp_path):
    root = copy_library(tmp_path)
    session, _ = make_session(root)
    created = create_client(session, "Probe Mercer", kind="project", client="acme-bakery")

    result = update(session, created, [{"op": "set_property", "key": "locations",
                                        "values": [f"vault password: {FAKE}"]}])

    assert result["ok"] is True, result
    (warning,) = secret_warnings(result)
    assert warning["area"] == "property"
    assert "section" not in warning
    assert warning["kind"] == ASSIGNMENT
    assert FAKE not in json.dumps(result)


def test_an_update_warns_for_a_replaced_section(tmp_path):
    root = copy_library(tmp_path)
    session, _ = make_session(root)
    created = create_client(session, "Probe Cooper")

    result = update(session, created, [{"op": "replace_section", "section": "Current work",
                                        "body": f"Handover\nBearer {FAKE}"}])

    assert result["ok"] is True, result
    (warning,) = secret_warnings(result)
    assert warning["kind"] == "bearer token"
    assert FAKE not in json.dumps(result)


def test_an_update_warns_for_a_replaced_line(tmp_path):
    root = copy_library(tmp_path)
    session, _ = make_session(root)
    created = create_client(session, "Probe Weaver", body="Handover: none")

    result = update(session, created, [{"op": "replace_line", "section": "Systems of record",
                                        "match": "Handover: none", "text": f"password: {FAKE}"}])

    assert result["ok"] is True, result
    (warning,) = secret_warnings(result)
    assert warning["kind"] == ASSIGNMENT
    assert FAKE not in json.dumps(result)


def test_an_update_warns_for_a_credential_in_its_source(tmp_path):
    root = copy_library(tmp_path)
    session, _ = make_session(root)
    created = create_client(session, "Probe Draper")

    result = update(session, created, [{"op": "set_property", "key": "status", "value": "active"}],
                    source=f"token: {FAKE}")

    assert result["ok"] is True, result
    (warning,) = secret_warnings(result)
    assert warning["area"] == "change_log"
    assert warning["kind"] == ASSIGNMENT
    assert FAKE not in json.dumps(secret_warnings(result))


def test_a_returned_change_log_line_is_the_record_own_text(tmp_path):
    """``change_log_line`` repeats what was written; it is record content, as stored."""
    root = copy_library(tmp_path)
    session, _ = make_session(root)
    created = create_client(session, "Probe Fletcher")

    result = update(session, created, [{"op": "append_to_section", "section": "Current work", "text": "note"}],
                    source=f"token: {FAKE}")

    assert result["ok"] is True, result
    assert FAKE in result["data"]["change_log_line"]
    (warning,) = secret_warnings(result)
    assert warning["area"] == "change_log"
    assert FAKE not in json.dumps(warning)


def test_an_update_warns_for_a_credential_in_a_section_title(tmp_path):
    root = copy_library(tmp_path)
    session, _ = make_session(root)
    created = create_client(session, "Probe Sawyer")

    result = update(session, created, [{"op": "replace_section", "section": f"Bearer {FAKE}",
                                        "body": "first"}])

    assert result["ok"] is True, result
    warnings = secret_warnings(result)
    assert {warning["kind"] for warning in warnings} == {"bearer token"}
    # The returned change-log line names the section it wrote; a warning does not.
    assert FAKE not in json.dumps(secret_warnings(result))


def test_an_update_warns_for_a_second_credential_of_the_same_kind(tmp_path):
    """A record that already holds one password is not a place to hide another."""
    root = copy_library(tmp_path)
    session, _ = make_session(root)
    other = "Rt6Yb8Nc0Zq9x7Lm2Pw4"
    created = create_client(session, "Probe Tiler", body=f"portal\npassword: {FAKE}")

    result = update(session, created, [{"op": "append_to_section", "section": "Contacts",
                                       "text": f"password: {other}"}], source="intake")

    assert result["ok"] is True, result
    (warning,) = secret_warnings(result)
    assert warning["area"] == "section" and warning["section"] == 3
    assert warning["kind"] == ASSIGNMENT
    assert other not in json.dumps(result)


def test_an_update_that_leaves_an_existing_secret_alone_does_not_warn(tmp_path):
    root = copy_library(tmp_path)
    session, _ = make_session(root)
    created = create_client(session, "Probe Catering", body=f"password: {FAKE}")
    record = root / created["data"]["path"]

    result = update(session, created, [{"op": "set_property", "key": "status", "value": "active"}],
                    source="call notes")

    assert result["ok"] is True, result
    assert secret_warnings(result) == []
    assert FAKE in record.read_text(encoding="utf-8")


def test_an_update_that_restates_an_existing_secret_does_not_warn(tmp_path):
    """Replacing a section with its own unchanged text adds no new value."""
    root = copy_library(tmp_path)
    session, _ = make_session(root)
    created = create_client(session, "Probe Slater", body=f"portal\npassword: {FAKE}")

    result = update(session, created, [{"op": "replace_section", "section": "Systems of record",
                                        "body": f"portal\npassword: {FAKE}"}], source="reformat")

    assert result["ok"] is True, result
    assert secret_warnings(result) == []


def test_an_update_that_moves_an_existing_secret_does_not_warn(tmp_path):
    """The comparison is a set of findings, never a position."""
    root = copy_library(tmp_path)
    session, _ = make_session(root)
    created = create_client(session, "Probe Farrier", body=f"portal\npassword: {FAKE}")

    result = update(session, created, [
        {"op": "remove_line", "section": "Systems of record", "match": f"password: {FAKE}"},
        {"op": "append_to_section", "section": "Contacts", "text": f"password: {FAKE}"}], source="move")

    assert result["ok"] is True, result
    assert secret_warnings(result) == []


def test_an_update_that_reformats_a_value_onto_a_quoted_next_line_does_not_warn(tmp_path):
    """One value rule: a value is trimmed the same way on its own line and the next."""
    root = copy_library(tmp_path)
    session, _ = make_session(root)
    created = create_client(session, "Probe Reformat", body=f"password: {FAKE}")
    assert len(secret_warnings(created)) == 1

    result = update(session, created, [{"op": "replace_section", "section": "Systems of record",
                                        "body": f'password:\n  "{FAKE}"'}])

    assert result["ok"] is True, result
    assert secret_warnings(result) == []
    assert f'  "{FAKE}"' in (root / created["data"]["path"]).read_text(encoding="utf-8")


def test_an_update_that_adds_and_removes_a_credential_does_not_warn(tmp_path):
    """The saved record holds no credential, so nothing warns about one."""
    root = copy_library(tmp_path)
    session, _ = make_session(root)
    created = create_client(session, "Probe Bindery", body="CRM: Harbor")

    result = update(session, created, [
        {"op": "append_to_section", "section": "Systems of record", "text": f"password: {FAKE}"},
        {"op": "remove_line", "section": "Systems of record", "match": f"password: {FAKE}"}], source="typo")

    assert result["ok"] is True, result
    assert secret_warnings(result) == []
    assert FAKE not in (root / result["data"]["path"]).read_text(encoding="utf-8")


def test_an_update_that_assembles_a_credential_across_two_ops_warns(tmp_path):
    """The key and the value arrive in separate ops; the saved record holds both."""
    root = copy_library(tmp_path)
    session, _ = make_session(root)
    created = create_client(session, "Probe Glazier", body="CRM: Harbor")

    result = update(session, created, [
        {"op": "replace_section", "section": "Systems of record", "body": "password:"},
        {"op": "append_to_section", "section": "Systems of record", "text": f"  {FAKE}"}], source="two ops")

    assert result["ok"] is True, result
    (warning,) = secret_warnings(result)
    assert warning["kind"] == ASSIGNMENT
    assert FAKE not in json.dumps(result)


def test_an_update_that_removes_the_value_does_not_warn(tmp_path):
    root = copy_library(tmp_path)
    session, _ = make_session(root)
    created = create_client(session, "Probe Landscaping", body=f"portal login\npassword: {FAKE}")

    result = update(session, created, [{"op": "remove_line", "section": "Systems of record",
                                        "match": FAKE}], source="cleanup")

    assert result["ok"] is True, result
    assert secret_warnings(result) == []
    assert FAKE not in (root / created["data"]["path"]).read_text(encoding="utf-8")


def test_a_credential_shaped_replacement_match_is_not_new_material(tmp_path):
    """``match`` names a line already in the record; replacing it removes the value.

    A warning scan over the ops' ``match`` strings would warn here, and the value
    is gone from the saved record, so there is nothing to warn about.
    """
    root = copy_library(tmp_path)
    session, _ = make_session(root)
    created = create_client(session, "Probe Signage", body=f"portal\npassword: {FAKE}")

    result = update(session, created, [{"op": "replace_line", "section": "Systems of record",
                                        "match": f"password: {FAKE}",
                                        "text": "Credentials: password manager collection Harbor"}],
                    source="cleanup")

    assert result["ok"] is True, result
    assert secret_warnings(result) == []
    assert FAKE not in (root / created["data"]["path"]).read_text(encoding="utf-8")
    assert [finding for finding in doctor(root).findings if finding.code == "secret_shaped"] == []


def test_a_refused_update_carries_no_secret_warning(tmp_path):
    """Nothing was saved, so there is nothing to warn about."""
    root = copy_library(tmp_path)
    session, _ = make_session(root)
    created = create_client(session, "Probe Roper")

    result = call("engagements_update", {
        "id": created["data"]["id"], "base_rev": "0" * 16, "source": "chat",
        "ops": [{"op": "append_to_section", "section": "Current work", "text": f"password: {FAKE}"}]}, session)

    assert result["ok"] is False, result
    assert secret_warnings(result) == []
    assert FAKE not in json.dumps(result)


# --- doctor keeps reporting what a write saved ---------------------------------


def test_doctor_reports_a_saved_secret_by_file_and_line_until_it_is_removed(tmp_path):
    root = copy_library(tmp_path)
    session, _ = make_session(root)
    created = create_client(session, "Probe Cleano", body=f"note\npassword: {FAKE}")
    relpath = created["data"]["path"]
    line = record_line(root, relpath, FAKE)

    report = doctor(root)

    hits = [finding for finding in report.findings if finding.code == "secret_shaped"]
    assert [(finding.path, finding.line) for finding in hits] == [(relpath, line)]
    assert hits[0].area == "section" and hits[0].section_index == 6
    assert FAKE not in json.dumps(report.as_dict())
    assert FAKE not in report.render_text()

    removed = update(session, created, [{"op": "remove_line", "section": "Systems of record",
                                         "match": FAKE}], source="cleanup")
    assert removed["ok"] is True, removed

    assert [finding for finding in doctor(root).findings if finding.code == "secret_shaped"] == []


def test_doctor_reads_a_value_on_the_line_after_its_key(tmp_path):
    """Doctor reads the record as a whole, so a value on its own line is still found."""
    root = copy_library(tmp_path)
    session, _ = make_session(root)
    created = create_client(session, "Probe Mason", body=f"portal\npassword:\n  {FAKE}")
    relpath = created["data"]["path"]
    line = record_line(root, relpath, FAKE)

    hits = [finding for finding in doctor(root).findings if finding.code == "secret_shaped"]

    assert [(finding.path, finding.line) for finding in hits] == [(relpath, line)]
    assert FAKE not in json.dumps([finding.as_dict() for finding in hits])


def test_doctor_reports_every_kind_by_kind_name(tmp_path):
    root = copy_library(tmp_path)
    session, _ = make_session(root)
    create_client(session, "Probe Chandler", body="\n".join([
        f"password: {SHORT}",
        "-----BEGIN " + "RSA PRIVATE KEY-----",
        "key " + "AKIA" + "AB12" * 4,
        "key sk_live_" + FAKE,
        "token ghp_" + HEXLIKE + HEXLIKE,
        "Authorization: Bearer " + FAKE + "Yb8",
        "https://acme:" + FAKE + "@files.example/share",
        "https://hooks.slack.com/services/T000/B000/" + FAKE,
    ]))

    kinds = [finding.message.split("(")[1].split(")")[0]
             for finding in doctor(root).findings if finding.code == "secret_shaped"]

    assert kinds == ["password or secret assignment", "private key", "cloud access key", "API key",
                     "access token", "bearer token", "URL with embedded credentials", "webhook URL"]


# --- the promise: no text from the record in a warning or a secret finding -----


EVERY_KIND_BODY = "\n".join([
    f"portal password: {FAKE}",
    "-----BEGIN " + "RSA PRIVATE KEY-----",
    "cloud " + "AKIA" + "AB12" * 4,
    "stripe sk_live_" + FAKE,
    "github ghp_" + HEXLIKE + HEXLIKE,
    "Authorization: Bearer " + FAKE + "Yb8",
    "https://acme:" + FAKE + "@files.example/share",
    "https://hooks.slack.com/services/T000/B000/" + FAKE,
])
MADE_UP_SECRETS = (FAKE, HEXLIKE + HEXLIKE, "AKIA" + "AB12" * 4, "sk_live_" + FAKE,
                   "ghp_" + HEXLIKE + HEXLIKE, FAKE + "Yb8")


def _windows(value, size=6):
    return {value[index:index + size] for index in range(len(value) - size + 1)}


def _assert_no_record_text(text, record, secrets):
    """``text`` holds no line of ``record`` and no 6-character window of a made-up secret."""
    for line in record.split("\n"):
        if line.strip():
            assert line not in text, line
    for secret in secrets:
        for window in _windows(secret):
            assert window not in text, window


def _protected_doctor_output(root):
    """The secret findings, selected from the complete JSON and text doctor output.

    Both outputs are serialised whole first and the secret findings are picked out
    of the serialisation, so nothing is filtered before it is rendered.
    """
    report = doctor(root)
    complete_json = json.loads(json.dumps(report.as_dict(), ensure_ascii=False))
    complete_text = report.render_text()
    findings = [finding for finding in complete_json["findings"] if finding["code"] == "secret_shaped"]
    lines = [line for line in complete_text.split("\n") if " secret_shaped " in line]
    return findings, lines


def test_a_warning_and_a_doctor_finding_carry_no_text_from_the_record(tmp_path):
    root = copy_library(tmp_path)
    session, _ = make_session(root)

    created = create_client(session, "Probe Confidential", body=EVERY_KIND_BODY)

    assert created["ok"] is True, created
    relpath = created["data"]["path"]
    record = (root / relpath).read_text(encoding="utf-8")
    complete_result = json.loads(json.dumps(created, ensure_ascii=False))
    warnings = [entry for entry in complete_result["warnings"] if entry["code"] == POSSIBLE_SECRET_CODE]
    findings, lines = _protected_doctor_output(root)

    # Eight findings: five listed in file order, three counted.
    assert [entry.get("kind") for entry in warnings] == list(KINDS[:5]) + [None]
    assert warnings[-1]["omitted"] == 3
    assert [finding["kind"] for finding in findings] == list(KINDS)
    assert len(lines) == 8
    # The warning holds no record-derived field at all.
    _assert_no_record_text(json.dumps(warnings, ensure_ascii=False), record, MADE_UP_SECRETS)
    # A doctor secret finding holds one: the record's path, as its identity.
    for finding in findings:
        assert finding.pop("path") == relpath
        assert set(finding) == {"severity", "code", "message", "line", "area", "section_index", "kind",
                                "occurrence"}
    _assert_no_record_text(json.dumps(findings, ensure_ascii=False), record, MADE_UP_SECRETS)
    for line in lines:
        assert f"  {relpath}:" in line
        _assert_no_record_text(line.replace(relpath, ""), record, MADE_UP_SECRETS)


def test_a_credential_in_the_record_name_is_in_the_path_and_nowhere_else(tmp_path):
    """The documented exception: a record's id, path and name are its own identity.

    The save succeeds. The warning carries no record text, so not the name either.
    Doctor's secret finding carries the record's path, which holds the name, and
    the value appears in that field and nowhere else in the finding, in JSON and
    in text output.
    """
    root = copy_library(tmp_path)
    session, _ = make_session(root)
    value = f"sk_live_{FAKE}"

    result = create_client(session, f"Probe {value}")

    assert result["ok"] is True, result
    relpath = result["data"]["path"]
    assert value in relpath
    warnings = secret_warnings(result)
    assert warnings and {entry["kind"] for entry in warnings} == {API_KEY}
    assert warnings[0]["area"] == "property"
    for window in _windows(value):
        assert window not in json.dumps(warnings)
    findings, lines = _protected_doctor_output(root)
    assert len(findings) == len(warnings) == len(lines)
    for finding, line in zip(findings, lines, strict=True):
        assert finding["path"] == relpath
        rest = {name: item for name, item in finding.items() if name != "path"}
        assert value in line
        for window in _windows(value):
            assert window not in json.dumps(rest)
            assert window not in line.replace(relpath, "")
        assert finding["kind"] == API_KEY


def test_doctor_reports_two_findings_of_one_kind_on_one_line(tmp_path):
    """Every finding is a doctor finding; occurrence tells them apart, never the text."""
    root = copy_library(tmp_path)
    session, _ = make_session(root)
    created = create_client(session, "Probe Twofold", body=f"password: {FAKE} token: {SHORT}{FAKE}")
    relpath = created["data"]["path"]
    line = record_line(root, relpath, FAKE)

    warnings = secret_warnings(created)
    findings, _lines = _protected_doctor_output(root)

    assert len(warnings) == 2
    assert len(findings) == 2
    assert [(entry["line"], entry["kind"], entry["occurrence"]) for entry in warnings] == [
        (line, ASSIGNMENT, 1), (line, ASSIGNMENT, 2)]
    assert [(finding["line"], finding["kind"], finding["occurrence"]) for finding in findings] == [
        (line, ASSIGNMENT, 1), (line, ASSIGNMENT, 2)]
    assert findings[0]["message"] != findings[1]["message"]


# --- the message ---------------------------------------------------------------


def test_the_warning_is_one_stern_message_plus_the_location(tmp_path):
    root = copy_library(tmp_path)
    session, _ = make_session(root)

    result = create_client(session, "Probe Bindery", body=f"password: {FAKE}")

    (warning,) = secret_warnings(result)
    assert warning["message"] == POSSIBLE_SECRET_MESSAGE.format(
        location=f"line {warning['line']} of section 6, finding 1 on that line")
    assert "The record was saved." in warning["message"]
    assert "Stop and tell the user now, before any other action." in warning["message"]
    assert "rotate it if it was real" in warning["message"]
    assert "must never hold secrets" in warning["message"]
    assert FAKE not in json.dumps(result)


# --- growth: four times the input costs less than eight times the time ---------
#
# Every timed sample is a successful create into a fresh copy of the same library,
# so each one runs the whole write path and the detector on a record of the same
# shape; each result is asserted. The guard is a ratio, never an absolute time.


def _best_successful_create(tmp_path, label, sections, attempts=3):
    best = float("inf")
    for attempt in range(attempts):
        root = copy_library(tmp_path / f"{label}-{attempt}")
        session, _ = make_session(root)
        args = {"kind": "client", "name": "Growth Probe Co", "sections": sections}
        gc.collect()  # so no sample pays for garbage left by the one before it
        began = time.perf_counter()
        result = call("engagements_create", args, session)
        elapsed = time.perf_counter() - began
        assert result["ok"] is True, result
        assert (root / result["data"]["path"]).stat().st_size > sum(len(item["body"]) for item in sections)
        best = min(best, elapsed)
    return best


def _thirty_sections(build, size):
    return [{"section": f"Note {index}", "body": build(size)} for index in range(30)]


def _filler(size):
    return ("Record note. " * (size // 13 + 1))[:size]


def _repeated_assignments(size):
    return (f"password: {SHORT} " * (size // 15 + 1))[:size]


def _empty_keys_then(character):
    # The quadratic input: many empty assignments on one line
    # share one more-indented next line.
    def build(size):
        return "password: " * (size // 20) + "\n " + character * (size // 2 - 2)
    return build


GROWTH_BODIES = [
    pytest.param(_filler, id="plain-text"),
    pytest.param(_repeated_assignments, id="repeated-assignments"),
    pytest.param(_empty_keys_then("q"), id="empty-keys-share-a-long-next-line"),
    pytest.param(_empty_keys_then("x"), id="empty-keys-share-a-placeholder-next-line"),
]


@pytest.mark.parametrize("build", GROWTH_BODIES)
def test_thirty_sections_through_the_public_create_path_grow_linearly(tmp_path, build):
    """Thirty sections of 5,000 and of 20,000 characters, each a successful create."""
    small = _best_successful_create(tmp_path, "small", _thirty_sections(build, 5_000))
    large = _best_successful_create(tmp_path, "large", _thirty_sections(build, 20_000))
    assert large < small * 8, f"30 x 5,000 {small:.3f}s, 30 x 20,000 {large:.3f}s"


def test_five_hundred_findings_are_one_pass_and_stay_bounded(tmp_path):
    def sections(count):
        body = "\n".join(f"password: {FAKE}{number:04d}" for number in range(count))
        return [{"section": "Systems of record", "body": body}]

    small = _best_successful_create(tmp_path, "small", sections(125))
    large = _best_successful_create(tmp_path, "large", sections(500))
    assert large < small * 8, f"125 findings {small:.3f}s, 500 findings {large:.3f}s"

    root = copy_library(tmp_path / "bounded")
    session, _ = make_session(root)
    result = call("engagements_create", {"kind": "client", "name": "Zeta Many Co", "sections": sections(500)},
                  session)
    assert result["ok"] is True, result
    warnings = secret_warnings(result)
    assert len(warnings) == 6
    assert warnings[-1]["omitted"] == 495
    assert set(warnings[-1]) == {"code", "message", "omitted"}
