"""A resolved record identity must still hold when the handler reads or writes.

Resolving an id against discovery and then handing only a path to
the writer (or to a read) is two observations of the library. When the path
changes identity in between, the old code applied the update to, or returned the
body of, a record the caller never asked for.

Fictional data only.
"""

import json

from tools_support import call, copy_library, make_session

ACME = "Acme Bakery/Client.md"

OTHER = """---
engagements: client
id: cedar-hill-cafe
name: Cedar Hill Cafe
status: active
updated: 2026-11-05
---

## Summary
Another fictional client.

## Change log
- 2026-11-05 studio-dev: created client (source: test)
"""


def replace_with_other_client(root):
    """Make the Acme path hold a different client: same kind, different id and name.

    The replacement id is unique, so a library-wide defect cannot mask the
    identity check with the ``malformed`` precedence TOOLS.md gives stage 4.
    """
    source = OTHER.encode()
    (root / ACME).write_bytes(source)
    return source


def read_rev(session, identifier="acme-bakery"):
    result = call("engagements_context", {"id": identifier}, session)
    assert result["ok"], result
    return result["data"]["rev"]


def status_update(session, base_rev, identifier="acme-bakery", value="paused"):
    return call("engagements_update", {"id": identifier, "base_rev": base_rev, "source": "test 2026-11-07",
                                       "ops": [{"op": "set_property", "key": "status", "value": value}]}, session)


# --- update ------------------------------------------------------------------


def test_an_update_refuses_a_record_whose_id_changed_after_resolution(tmp_path, monkeypatch):
    root = copy_library(tmp_path)
    session, state = make_session(root)
    base = read_rev(session)

    from engagements import writer
    original = writer.update
    swapped = {}

    def racing(*args, **kwargs):
        # The path holds another record before the writer's pre-lock read.
        swapped["bytes"] = replace_with_other_client(root)
        return original(*args, **kwargs)

    monkeypatch.setattr(writer, "update", racing)
    result = status_update(session, base)

    assert result["ok"] is False, result
    assert result["error"]["code"] == "conflict"
    details = result["error"]["details"]
    assert details["reason"] == "footprint_changed"
    assert [footprint["key"] for footprint in details["footprints"]] == ["id"]
    # Nothing was applied to the record that took the path.
    assert (root / ACME).read_bytes() == swapped["bytes"]
    assert state == {}


def test_an_update_refuses_an_identity_swap_between_the_prelock_and_locked_reads(tmp_path, monkeypatch):
    root = copy_library(tmp_path)
    session, _ = make_session(root)
    base = read_rev(session)

    from engagements import records, writer
    original = records.validate_values
    calls = {"n": 0}

    def racing(kind, ops, *, client_exists):
        calls["n"] += 1
        if calls["n"] == 1:
            # After the pre-lock read and its stage-4 checks, before the lock is taken.
            replace_with_other_client(root)
        return original(kind, ops, client_exists=client_exists)

    monkeypatch.setattr(writer.records, "validate_values", racing)
    result = status_update(session, base)

    assert result["ok"] is False, result
    assert result["error"]["code"] == "conflict"
    assert result["error"]["details"]["reason"] == "footprint_changed"
    assert (root / ACME).read_bytes() == OTHER.encode()


def test_a_renamed_record_is_still_updated_by_id(tmp_path):
    """Identity is id and kind: a name change must not be mistaken for a swap."""
    root = copy_library(tmp_path)
    path = root / ACME
    path.write_text(path.read_text(encoding="utf-8").replace("name: Acme Bakery", "name: Acme Bakery Group", 1),
                    encoding="utf-8")
    session, _ = make_session(root)
    base = read_rev(session)

    result = status_update(session, base)

    assert result["ok"] is True, result
    assert result["data"]["id"] == "acme-bakery"
    assert "status: paused" in path.read_text(encoding="utf-8")


# --- read --------------------------------------------------------------------


def test_a_read_by_id_refuses_a_record_whose_id_changed_after_resolution(tmp_path, monkeypatch):
    root = copy_library(tmp_path)
    session, _ = make_session(root)

    from engagements import writer
    original = writer.read_record

    def racing(*args, **kwargs):
        replace_with_other_client(root)
        return original(*args, **kwargs)

    monkeypatch.setattr(writer, "read_record", racing)
    result = call("engagements_context", {"id": "acme-bakery"}, session)

    assert result["ok"] is False, result
    assert result["error"]["code"] == "not_found"
    assert result["error"]["details"] == {"reason": "no_match"}
    assert "Cedar Hill" not in json.dumps(result)


def test_a_project_read_by_id_refuses_a_kind_swap(tmp_path, monkeypatch):
    root = copy_library(tmp_path)
    session, _ = make_session(root)
    project = "Acme Bakery/Projects/Website 2026.md"

    from engagements import writer
    original = writer.read_record

    def racing(*args, **kwargs):
        (root / project).write_bytes((root / ACME).read_bytes())
        return original(*args, **kwargs)

    monkeypatch.setattr(writer, "read_record", racing)
    result = call("engagements_context", {"id": "acme-website-2026"}, session)

    assert result["ok"] is False, result
    assert result["error"]["code"] == "not_found"
    assert "Acme Bakery/Projects/Website 2026.md" not in json.dumps(result)


def test_a_read_by_path_is_not_identity_checked(tmp_path):
    """An explicit path is the caller's own selection, so its current contents are the answer."""
    root = copy_library(tmp_path)
    replace_with_other_client(root)
    session, _ = make_session(root)

    result = call("engagements_context", {"path": ACME}, session)

    assert result["ok"] is True, result
    assert result["data"]["id"] == "cedar-hill-cafe"
