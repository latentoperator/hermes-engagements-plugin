"""Diagnostics report where a secret-shaped value is, never the value.

Doctor sanitized the secret warning itself, but the same
secret-shaped text still reached ``key``, ``section`` and several messages
(headings, unsupported frontmatter keys, project ids in shared-location text),
including the plain-text rendering. Tool warnings quoted defective keys and
headings the same way.

Values are assembled at runtime so no real-looking token sits in the source.
Fictional data only.
"""

import json
import shutil

import pytest
from conftest import FIXTURE_LIBRARY, record_bytes, write
from engagements import tools
from engagements.doctor import run_doctor
from tools_support import binding_key_for, call, copy_library, make_session

TOKEN = "sk-" + "abc123def456ghi789jkl012"  # 24 body characters: matches the token pattern
REDACTED = "[redacted]"


def doctor(root, **kwargs):
    kwargs.setdefault("home", "/home/tester")
    kwargs.setdefault("os_name", "posix")
    kwargs.setdefault("exists", lambda path: True)
    kwargs.setdefault("realpath", lambda path: path)
    return run_doctor(root, **kwargs)


def assert_never_echoed(report, *values):
    rendered = report.render_text()
    dumped = json.dumps(report.as_dict())
    for value in values:
        assert value not in rendered
        assert value not in dumped


def make_library(root):
    write(root, ".engagements/library.yaml", "format: 1\nlibrary_id: 44444444-4444-4444-8444-444444444444\n")
    return root


def project_bytes(project_id, client, location):
    return "\n".join([
        "---", "engagements: project", f"id: {project_id}", f"client: {client}", "name: Shared",
        "status: active", f"locations: [{location}]", "updated: 2026-11-05", "---", "", "## Purpose", "",
    ]).encode()


# --- doctor ------------------------------------------------------------------


def test_a_secret_shaped_heading_is_redacted_from_section_and_text(tmp_path):
    root = make_library(tmp_path / "library")
    write(root, "Redact/Headings.md", "\n".join([
        "---", "engagements: client", "id: redaction-heading", "name: Redaction Heading", "---", "",
        f"## {TOKEN}", "one", "",
        f"## {TOKEN}", "two", "",
    ]).encode())

    report = doctor(root)

    flagged = [f for f in report.findings if f.code == "duplicate_section"]
    assert len(flagged) == 2
    assert all(f.section == REDACTED for f in flagged)
    assert all(f.path == "Redact/Headings.md" and f.line for f in flagged)
    assert_never_echoed(report, TOKEN)


def test_a_secret_shaped_property_key_is_redacted(tmp_path):
    root = make_library(tmp_path / "library")
    write(root, "Redact/Keys.md", "\n".join([
        "---", "engagements: client", "id: redaction-key", "name: Redaction Key",
        f'"{TOKEN}": one', "---", "", "## Summary", "",
    ]).encode())

    report = doctor(root)

    (finding,) = [f for f in report.findings if f.code == "unsupported_yaml"]
    assert finding.key == REDACTED
    assert finding.line == 5 and finding.message
    assert_never_echoed(report, TOKEN)


def test_a_secret_shaped_project_id_is_redacted_from_shared_location_text(tmp_path):
    root = make_library(tmp_path / "library")
    write(root, "Acme Bakery/Client.md", record_bytes("client", "acme-bakery", "Acme Bakery"))
    write(root, "Secret/Client.md", record_bytes("client", "secret-client", "Secret Client"))
    write(root, "Acme Bakery/Projects/Shared.md", project_bytes("acme-shared", "acme-bakery", "/w/shared"))
    write(root, "Secret/Projects/Shared.md", project_bytes(TOKEN, "secret-client", "/w/shared"))

    report = doctor(root)

    shared = [f for f in report.findings if f.code == "shared_location"]
    assert {f.path for f in shared} == {"Acme Bakery/Projects/Shared.md", "Secret/Projects/Shared.md"}
    acme = next(f for f in shared if f.path.startswith("Acme"))
    assert REDACTED in acme.message
    # The other project's id is not secret-shaped, so the reason still names it.
    assert "acme-shared" in next(f for f in shared if f.path.startswith("Secret")).message
    assert_never_echoed(report, TOKEN)


def test_a_secret_shaped_heading_and_key_do_not_leak_from_any_output(tmp_path):
    root = tmp_path / "library"
    shutil.copytree(FIXTURE_LIBRARY, root)
    write(root, "Redact/Both.md", "\n".join([
        "---", "engagements: client", "id: redaction-both", "name: Redaction Both",
        f'"{TOKEN}": one', "---", "",
        f"## {TOKEN}", "one", "",
        f"## {TOKEN}", "two", "",
    ]).encode())

    report = doctor(root)

    assert_never_echoed(report, TOKEN)
    assert report.checked == 6
    assert any(f.code == "duplicate_section" and f.severity == "warning" for f in report.findings)


# --- tools -------------------------------------------------------------------


def tools_record():
    return "\n".join([
        "---", "engagements: client", "id: redaction-tools", "name: Redaction Tools",
        f'"{TOKEN}": one', "---", "",
        f"## {TOKEN}", "one", "",
        f"## {TOKEN}", "two", "",
    ]).encode()


def test_context_warnings_redact_a_defective_key_and_heading(tmp_path):
    root = copy_library(tmp_path)
    write(root, "Redact/Record.md", tools_record())
    session, _ = make_session(root)

    result = call("engagements_context", {"path": "Redact/Record.md"}, session)

    assert result["ok"] is True, result
    # The body is data and may contain the heading (a record can hold anything);
    # diagnostics never echo it.
    diagnostics = json.dumps({"warnings": result["warnings"]})
    assert TOKEN not in diagnostics
    messages = [warning["message"] for warning in result["warnings"]]
    assert any(REDACTED in message for message in messages)
    assert any(warning["code"] == "duplicate_section" for warning in result["warnings"])
    # Still locates the record and the reason.
    assert all(warning["path"] == "Redact/Record.md" for warning in result["warnings"])


def test_an_unknown_id_is_redacted_in_the_not_found_message(tmp_path):
    root = copy_library(tmp_path)
    session, _ = make_session(root)

    result = call("engagements_update", {"id": TOKEN, "base_rev": "0" * 16, "source": "test",
                                         "ops": [{"op": "set_property", "key": "status", "value": "paused"}]},
                  session)

    assert result["error"]["code"] == "not_found"
    assert TOKEN not in json.dumps(result)
    assert REDACTED in result["error"]["message"]


def test_a_secret_shaped_bind_argument_is_redacted_when_it_does_not_resolve(tmp_path):
    root = copy_library(tmp_path)
    session, _ = make_session(root)

    result = call("engagements_bind", {"client": TOKEN}, session)

    assert result["error"]["code"] == "not_found"
    assert TOKEN not in json.dumps(result)
    assert REDACTED in result["error"]["message"]


def test_a_secret_shaped_project_id_is_redacted_in_a_bind_error(tmp_path):
    root = copy_library(tmp_path)
    write(root, "Secret/Projects/Shared.md", project_bytes(TOKEN, "riverbend-dental", "/w/shared"))
    session, state = make_session(root)

    result = call("engagements_bind", {"client": "acme-bakery", "project": TOKEN}, session)

    assert result["error"]["code"] == "invalid_binding"
    assert TOKEN not in json.dumps(result)
    assert REDACTED in result["error"]["message"]
    assert state == {}


# --- the whole diagnostic structure, at any depth -----------------------------

ACCESS_KEY = "AKIA" + "AB12" * 4
GITHUB_TOKEN = "ghp_" + "abcdef0123456789" * 2
JWT = "eyJ" + "AbCd1234" * 2 + "." + "MnOp5678" * 2 + "." + "WxYz9012" * 2
PRIVATE_KEY = ("-----BEGIN RSA PRIVATE KEY-----\n"
               "MIIBsyntheticbodyline\n"
               "-----END RSA PRIVATE KEY-----")

# (shape written into a record, the value that must never survive, line-safe, id-safe)
SHAPES = [
    pytest.param(ACCESS_KEY, ACCESS_KEY, True, True, id="access-key"),
    pytest.param(GITHUB_TOKEN, GITHUB_TOKEN, True, True, id="github-token"),
    pytest.param(JWT, JWT, True, True, id="jwt"),
    pytest.param(TOKEN, TOKEN, True, True, id="api-token"),
    pytest.param("password: SyntheticCredential123", "SyntheticCredential123", True, False, id="assignment"),
    pytest.param(PRIVATE_KEY, "MIIBsyntheticbodyline", False, False, id="private-key"),
]


def strings_in(value):
    """Every key and value in a diagnostic structure, however deeply nested."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from strings_in(key)
            yield from strings_in(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from strings_in(item)


def assert_shape_absent(payload, probe, *extra):
    for text in strings_in(payload):
        for value in (probe, *extra):
            assert value.casefold() not in text.casefold(), (value, text)


def shape_record(shape, line_safe):
    """A record whose unsupported key, and optionally duplicated headings, quote ``shape``."""
    lines = ["---", "engagements: client", "id: shape-record", "name: Shape Record",
             json.dumps(shape) + ": one", "---", "", "## Summary", ""]
    if line_safe:
        lines += [f"## {shape}", "one", "", f"## {shape}", "two", ""]
    return "\n".join(lines).encode()


@pytest.mark.parametrize("shape,probe,line_safe,id_safe", SHAPES)
def test_doctor_never_echoes_a_secret_shape_from_a_key(tmp_path, shape, probe, line_safe, id_safe):
    root = make_library(tmp_path / "library")
    write(root, "Shapes/Key.md", shape_record(shape, line_safe=False))

    report = doctor(root)

    assert_shape_absent(report.as_dict(), probe)
    assert_shape_absent(report.render_text(), probe)


@pytest.mark.parametrize("shape,probe,line_safe,id_safe", SHAPES)
def test_doctor_never_echoes_a_secret_shape_from_a_heading(tmp_path, shape, probe, line_safe, id_safe):
    if not line_safe:
        pytest.skip("a multiline value cannot be a heading")
    root = make_library(tmp_path / "library")
    write(root, "Shapes/Heading.md", shape_record(shape, line_safe=True))

    report = doctor(root)

    assert any(f.code == "duplicate_section" for f in report.findings), report.as_dict()
    assert_shape_absent(report.as_dict(), probe)
    assert_shape_absent(report.render_text(), probe)


@pytest.mark.parametrize("shape,probe,line_safe,id_safe", SHAPES)
def test_doctor_never_echoes_a_secret_shaped_project_id(tmp_path, shape, probe, line_safe, id_safe):
    if not id_safe:
        pytest.skip("the shape cannot be written as a YAML id")
    root = make_library(tmp_path / "library")
    write(root, "Acme Bakery/Client.md", record_bytes("client", "acme-bakery", "Acme Bakery"))
    write(root, "Seed/Client.md", record_bytes("client", "seed-client", "Seed Client"))
    write(root, "Acme Bakery/Projects/Shared.md", project_bytes("acme-shared", "acme-bakery", "/w/shared"))
    write(root, "Seed/Projects/Shared.md", project_bytes(shape, "seed-client", "/w/shared"))

    report = doctor(root)

    assert any(f.code == "shared_location" for f in report.findings), report.as_dict()
    assert_shape_absent(report.as_dict(), probe)
    assert_shape_absent(report.render_text(), probe)


@pytest.mark.parametrize("shape,probe,line_safe,id_safe", SHAPES)
def test_tool_warnings_never_echo_a_secret_shape(tmp_path, shape, probe, line_safe, id_safe):
    root = copy_library(tmp_path)
    write(root, "Shapes/Record.md", shape_record(shape, line_safe))
    session, _ = make_session(root)

    result = call("engagements_context", {"path": "Shapes/Record.md"}, session)

    assert result["ok"] is True, result
    assert result["warnings"], result
    assert_shape_absent({"warnings": result["warnings"]}, probe)


@pytest.mark.parametrize("shape,probe,line_safe,id_safe", SHAPES)
def test_error_details_never_echo_a_secret_shape_at_any_depth(tmp_path, shape, probe, line_safe, id_safe):
    root = copy_library(tmp_path)
    session, _ = make_session(root)

    # A rejected argument name lands in error.details.field.
    rejected = call("engagements_find", {"query": "acme", shape: True}, session)
    assert rejected["error"]["code"] == "invalid_op"
    assert_shape_absent({"error": rejected["error"]}, probe)

    # An unexpected nested diagnostic field is sanitized by default, not passed through.
    payload = tools._failure(
        "not_found", "No record.",
        {"new_field": {"deeper": [{"value": shape}, {shape: "a key"}]}},
        [], tools._roots(session))
    assert_shape_absent({"error": payload["error"]}, probe)


def test_nested_auto_evidence_is_redacted(tmp_path):
    """``error.details.auto.evidence`` quotes the project id auto resolution found."""
    root = copy_library(tmp_path)
    location = tmp_path / "synthetic-location"
    location.mkdir()
    write(root, "Synthetic/Project.md", project_bytes(TOKEN, "acme-bakery", str(location)))
    session, state = make_session(root, cwd=str(location))
    state[binding_key_for(root, "sess-1")] = {"mode": "off"}

    result = call("engagements_context", {}, session)

    assert result["error"]["code"] == "unbound"
    assert TOKEN not in json.dumps(result)
    assert REDACTED in result["error"]["details"]["auto"]["evidence"]


def test_an_unexpected_argument_name_is_redacted_in_error_details(tmp_path):
    root = copy_library(tmp_path)
    session, _ = make_session(root)

    result = call("engagements_find", {"query": "acme", TOKEN: True}, session)

    assert result["error"]["code"] == "invalid_op"
    assert result["error"]["details"]["reason"] == "unexpected_field"
    assert result["error"]["details"]["field"] == REDACTED


# --- contract bodies are returned verbatim ------------------------------------


def test_a_conflict_keeps_full_footprints_even_for_absolute_paths(tmp_path):
    """Path scrubbing is for diagnostics; a footprint is the contract's body."""
    root = copy_library(tmp_path)
    record = root / "Acme Bakery/Client.md"
    record.write_text(record.read_text(encoding="utf-8").replace(
        "~/clients/acme/docs", "/srv/acme/docs", 1), encoding="utf-8")
    session, _ = make_session(root)
    base = call("engagements_context", {"id": "acme-bakery"}, session)
    record.write_text(record.read_text(encoding="utf-8").replace(
        "/srv/acme/docs", "/srv/riverbend/docs", 1), encoding="utf-8")

    op = {"op": "append_to_section", "section": "Locations", "text": "- New reference"}
    result = call("engagements_update",
                  {"id": "acme-bakery", "base_rev": base["data"]["rev"], "source": "test", "ops": [op]}, session)

    assert result["error"]["code"] == "conflict", result
    (footprint,) = result["error"]["details"]["footprints"]
    assert footprint["key"] == "locations"
    assert "/srv/acme/docs" in footprint["base_text"]
    assert "/srv/riverbend/docs" in footprint["current_text"]
    assert footprint["base_text"] != footprint["current_text"]


# --- a flagged label never comes back in part ---------------------------------
#
# Both labels below are already flagged by the detector. An earlier check still let their
# remainder through: the producer interpolated the raw label into prose, and the
# span redactor stopped an assignment at the first space and a private key at its
# header when no END marker followed. A flagged label is suppressed whole, and a
# span that cannot be bounded runs to the end of the text (fail closed).

PEM_HEADER = "-----BEGIN " + "RSA PRIVATE KEY-----"
QUOTED_PASSWORD = 'password: "SyntheticWord HorseBattery9"'
UNQUOTED_PASSWORD = "password: SyntheticWord HorseBattery9"
OPEN_PRIVATE_KEY = PEM_HEADER + "\nMIIBopenbodyline0123456789\nsecondbodylineABCDEF"

RESIDUAL = [
    pytest.param(QUOTED_PASSWORD, ("HorseBattery9", "SyntheticWord"), id="quoted-multiword-password"),
    pytest.param(UNQUOTED_PASSWORD, ("HorseBattery9", "SyntheticWord"), id="unquoted-multiword-password"),
    pytest.param(OPEN_PRIVATE_KEY, ("MIIBopenbodyline", "secondbodyline"), id="private-key-without-end"),
]


@pytest.mark.parametrize("shape,probes", RESIDUAL)
def test_context_warnings_never_echo_the_rest_of_a_flagged_key(tmp_path, shape, probes):
    root = copy_library(tmp_path)
    write(root, "Shapes/Record.md", shape_record(shape, line_safe=False))
    session, _ = make_session(root)

    result = call("engagements_context", {"path": "Shapes/Record.md"}, session)

    assert result["ok"] is True, result
    assert result["warnings"], result
    assert_shape_absent({"warnings": result["warnings"]}, *probes)


@pytest.mark.parametrize("shape,probes", RESIDUAL)
def test_an_unexpected_argument_never_echoes_the_rest_of_a_flagged_name(tmp_path, shape, probes):
    root = copy_library(tmp_path)
    session, _ = make_session(root)

    result = call("engagements_find", {"query": "acme", shape: True}, session)

    assert result["error"]["code"] == "invalid_op"
    assert result["error"]["details"]["field"] == REDACTED
    assert_shape_absent({"error": result["error"]}, *probes)


@pytest.mark.parametrize("shape,probes", RESIDUAL)
def test_doctor_never_echoes_the_rest_of_a_flagged_key(tmp_path, shape, probes):
    root = make_library(tmp_path / "library")
    write(root, "Shapes/Key.md", shape_record(shape, line_safe=False))

    report = doctor(root)

    assert_shape_absent(report.as_dict(), *probes)
    assert_shape_absent(report.render_text(), *probes)


def test_a_footprint_body_is_exempt_only_on_a_conflict(tmp_path):
    """The verbatim exemption is the conflict contract's, not any error's."""
    root = copy_library(tmp_path)
    session, _ = make_session(root)

    payload = tools._failure(
        "invalid_op", "Invalid argument.",
        {"footprints": [{"key": "k", "base_text": TOKEN, "current_text": TOKEN}]},
        [], tools._roots(session))

    assert TOKEN not in json.dumps(payload)
