"""Every tool path is safe: no traceback, no outside absolute path, no record content or secret."""

import json
import os
import sys
from itertools import chain

import pytest
from tools_support import call, copy_library, make_session, rev_of

ACME = "Acme Bakery/Client.md"
ERROR_CODES = {
    "not_found", "ambiguous", "unbound", "invalid_binding", "conflict", "malformed", "invalid_op",
    "similar_exists", "outside_root", "lock_timeout", "library_missing", "no_session", "io_error",
}
WARNING_CODES = {"snapshot_failed", "maintenance_warning", "record_invalid", "malformed_skipped",
                 "library_created", "duplicate_section", "encoding_invalid", "possible_secret"}
# ``possible_secret`` names where the finding sits, structurally (docs/TOOLS.md);
# every other warning carries the common three fields only.
SECRET_WARNING_FIELDS = {"area", "section", "line", "kind", "occurrence", "omitted"}
SECRET_KEY = "AKIA" + "ABCDEFGHIJKLMNOP"
SECRET_PASSWORD = "hunter2" + "hunter2"
PARSER_EXCERPTS = ("while parsing", "could not find expected", "expected ','", 'in "<unicode string>"')
IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0

BROKEN = f"""---
engagements: client
id: broken-client
name: Broken Client
status: active
api_key: {SECRET_KEY}
password: {SECRET_PASSWORD}
broken: [unclosed
---

# Broken Client

## Summary
Bad body content 12345
"""


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from strings(item)


def assert_envelope(result):
    assert isinstance(result, dict), result
    assert isinstance(result.get("ok"), bool), result
    assert isinstance(result.get("warnings"), list), result
    for warning in result["warnings"]:
        assert isinstance(warning, dict)
        assert warning.get("code") in WARNING_CODES
        assert isinstance(warning.get("message"), str) and warning["message"]
        allowed = {"code", "message", "path"}
        if warning.get("code") == "possible_secret":
            allowed |= SECRET_WARNING_FIELDS
        assert set(warning) <= allowed
        if "path" in warning:
            assert isinstance(warning["path"], str)
    if result["ok"]:
        assert "data" in result and "error" not in result
    else:
        assert "error" in result and "data" not in result
        error = result["error"]
        assert set(error) >= {"code", "message", "details"}
        assert error["code"] in ERROR_CODES
        assert isinstance(error["message"], str) and error["message"]
        assert isinstance(error["details"], dict)
    json.dumps(result)  # the whole envelope is JSON-serializable


def assert_safe(result, tmp_path, root):
    text = json.dumps(result, ensure_ascii=False)
    assert "Traceback" not in text
    for marker in PARSER_EXCERPTS:
        assert marker not in text
    for secret in (SECRET_KEY, SECRET_PASSWORD):
        assert secret not in text
    library_root = result["data"].get("library_root") if result.get("ok") else None
    for chunk in strings(result):
        if library_root is not None and chunk == library_root:
            continue
        assert str(tmp_path) not in chunk, chunk
        assert "/etc/shadow" not in chunk, chunk
    for chunk in chain(strings(result.get("error")), strings(result.get("warnings"))):
        assert str(root) not in chunk, chunk


def check(result, tmp_path, root):
    assert_envelope(result)
    assert_safe(result, tmp_path, root)
    return result


def acme_rev(root):
    return rev_of(root / ACME)


# --- the whole call matrix ---------------------------------------------------


def test_every_tool_path_is_safe(tmp_path):
    root = copy_library(tmp_path)
    session, _ = make_session(root)
    absent, _ = make_session(tmp_path / "absent")
    outside = tmp_path / "outside" / "record.md"
    outside.parent.mkdir()
    outside.write_text("---\nengagements: client\nid: outsider\nname: Outsider\n---\n", encoding="utf-8")
    stale = "0" * 16

    cases = [
        ("find ok", "engagements_find", {"query": "acme"}, session, root),
        ("find bad args", "engagements_find", {}, session, root),
        ("find outside query", "engagements_find", {"query": str(outside)}, session, root),
        ("find missing library", "engagements_find", {"query": "acme"}, absent, tmp_path / "absent"),
        ("bind ok", "engagements_bind", {"client": "acme-bakery"}, session, root),
        ("bind unknown", "engagements_bind", {"client": "nobody"}, session, root),
        ("bind outside ref", "engagements_bind", {"client": str(outside)}, session, root),
        ("bind no session", "engagements_bind", {"client": "acme-bakery"},
         make_session(root, session_id=None)[0], root),
        ("context by id", "engagements_context", {"id": "acme-bakery"}, session, root),
        ("context by relative path", "engagements_context", {"path": ACME}, session, root),
        ("context by absolute inside path", "engagements_context", {"path": str(root / ACME)}, session, root),
        ("context outside path", "engagements_context", {"path": str(outside)}, session, root),
        ("context outside id", "engagements_context", {"id": str(outside)}, session, root),
        ("context unbound", "engagements_context", {}, make_session(root)[0], root),
        ("create ok", "engagements_create", {"kind": "client", "name": "Safety Matrix Co"}, session, root),
        ("create similar", "engagements_create", {"kind": "client", "name": "ACME bakery"}, session, root),
        ("update ok", "engagements_update", {"id": "acme-bakery", "base_rev": acme_rev(root),
                                            "source": "test 2026-11-07",
                                            "ops": [{"op": "set_property", "key": "status",
                                                     "value": "paused"}]}, session, root),
        ("update stale", "engagements_update", {"id": "acme-bakery", "base_rev": stale,
                                                "source": "test 2026-11-07",
                                                "ops": [{"op": "set_property", "key": "status",
                                                         "value": "paused"}]}, session, root),
        ("update unknown", "engagements_update", {"id": "nope-1", "base_rev": stale,
                                                  "source": "test 2026-11-07",
                                                  "ops": [{"op": "set_property", "key": "status",
                                                           "value": "paused"}]}, session, root),
    ]
    for label, name, args, caller, used_root in cases:
        result = call(name, args, caller)
        try:
            check(result, tmp_path, used_root)
        except AssertionError as exc:
            raise AssertionError(f"{label}: {exc}") from exc

    absolute_inside = call("engagements_context", {"path": str(root / ACME)}, session)
    assert absolute_inside["data"]["path"] == ACME
    found = call("engagements_find", {"query": "acme"}, session)
    assert found["ok"] and found["data"]["results"]
    assert all(not entry["path"].startswith("/") for entry in found["data"]["results"])


def test_a_malformed_record_leaks_no_parser_excerpt_or_secret(tmp_path):
    root = copy_library(tmp_path)
    broken = root / "Broken Client" / "Client.md"
    broken.parent.mkdir()
    broken.write_text(BROKEN, encoding="utf-8")
    session, _ = make_session(root)

    for label, name, args in (
        ("find", "engagements_find", {"query": "broken"}),
        ("context by path", "engagements_context", {"path": "Broken Client/Client.md"}),
        ("context by id", "engagements_context", {"id": "broken-client"}),
        ("update", "engagements_update", {"id": "broken-client", "base_rev": rev_of(broken),
                                          "source": "test 2026-11-07",
                                          "ops": [{"op": "set_property", "key": "status",
                                                   "value": "paused"}]}),
        ("create", "engagements_create", {"kind": "client", "name": "Another Co"}),
    ):
        result = call(name, args, session)
        try:
            check(result, tmp_path, root)
        except AssertionError as exc:
            raise AssertionError(f"{label}: {exc}") from exc
        assert "Bad body content 12345" not in json.dumps(result.get("error"))
        assert "Bad body content 12345" not in json.dumps(result.get("warnings"))

    read = call("engagements_context", {"path": "Broken Client/Client.md"}, session)
    assert read["ok"] is True
    assert read["data"]["valid"] is False
    assert any(warning["code"] == "record_invalid" for warning in read["warnings"])


@pytest.mark.skipif(IS_ROOT or sys.platform == "win32", reason="permission bits do not bind as root or on Windows")
def test_a_permission_failure_is_an_io_error(tmp_path):
    root = copy_library(tmp_path)
    target = root / "Riverbend Dental" / "Client.md"
    target.chmod(0)
    try:
        session, _ = make_session(root)
        result = call("engagements_context", {"path": "Riverbend Dental/Client.md"}, session)
    finally:
        target.chmod(0o644)
    assert result["ok"] is False
    assert result["error"]["code"] == "io_error"
    text = json.dumps(result, ensure_ascii=False)
    assert "Traceback" not in text
    assert "Permission" not in text
    assert "Errno" not in text
    assert str(root) not in text


def test_an_unexpected_exception_is_a_bare_io_error(tmp_path, monkeypatch):
    from engagements import library

    root = copy_library(tmp_path)
    session, _ = make_session(root)

    def boom(self):
        raise RuntimeError("boom /etc/shadow")

    monkeypatch.setattr(library.Library, "discover", boom)
    result = call("engagements_find", {"query": ""}, session)
    assert result["ok"] is False
    assert result["error"]["code"] == "io_error"
    assert result["error"]["message"] == "Filesystem operation failed."
    assert result["error"]["details"] == {}
    text = json.dumps(result)
    assert "boom" not in text and "/etc/shadow" not in text and "Traceback" not in text


def test_keyboard_interrupt_is_not_swallowed(tmp_path, monkeypatch):
    from engagements import library

    root = copy_library(tmp_path)
    session, _ = make_session(root)

    def interrupt(self):
        raise KeyboardInterrupt

    monkeypatch.setattr(library.Library, "discover", interrupt)
    with pytest.raises(KeyboardInterrupt):
        call("engagements_find", {"query": ""}, session)


# --- arguments and envelope invariants --------------------------------------


def test_an_unknown_tool_is_invalid_op(tmp_path):
    root = copy_library(tmp_path)
    session, _ = make_session(root)
    result = check(call("engagements_nope", {}, session), tmp_path, root)
    assert result["error"]["code"] == "invalid_op"
    assert result["error"]["details"]["field"] == "tool"
    assert result["error"]["details"]["reason"] == "invalid_value"


@pytest.mark.parametrize("name", ["engagements_find", "engagements_bind", "engagements_context",
                                  "engagements_create", "engagements_update"])
@pytest.mark.parametrize("args", [[], None, "acme"])
def test_arguments_must_be_an_object(tmp_path, name, args):
    root = copy_library(tmp_path)
    session, _ = make_session(root)
    result = check(call(name, args, session), tmp_path, root)
    assert result["error"]["code"] == "invalid_op"
    assert result["error"]["details"]["field"] == "arguments"
    assert result["error"]["details"]["reason"] == "invalid_value"


def test_envelope_invariants_hold_for_success_and_failure(tmp_path):
    root = copy_library(tmp_path)
    session, _ = make_session(root)
    results = [
        call("engagements_find", {"query": "acme"}, session),
        call("engagements_find", {}, session),
        call("engagements_context", {"id": "acme-bakery"}, session),
        call("engagements_context", {}, session),
        call("engagements_bind", {"mode": "off"}, session),
        call("engagements_create", {"kind": "client", "name": "Envelope Co"}, session),
        call("engagements_update", {"id": "acme-bakery", "base_rev": "0" * 16, "source": "test",
                                    "ops": [{"op": "set_property", "key": "status", "value": "paused"}]},
             session),
    ]
    for result in results:
        check(result, tmp_path, root)
    assert results[0]["ok"] is True and "data" in results[0]
    assert results[1]["ok"] is False and "error" in results[1]
