"""A binding must be settled against the bytes the response reports.

``bind`` used to resolve the target against discovery, decide identity,
status and the project/client relationship from those rows, persisted the
binding, and only then read the records for the response. A record changed
between discovery and the read produced either a binding to newly inactive
content, or a response whose ``status``/``name``/``evidence`` came from one
version while ``rev`` came from another. Bound reads had the same hole.

These tests change the library in the exact window the handler must close, using
the recorded-fiction library only.
"""

import json

from tools_support import call, copy_library, make_session

ACME = "Acme Bakery/Client.md"
PROJECT = "Acme Bakery/Projects/Website 2026.md"
OTHER = """---
engagements: client
id: cedar-hill-cafe
name: Cedar Hill Cafe
status: active
updated: 2026-11-05
---

## Summary
Another fictional client.
"""


def set_status(root, relpath, value):
    path = root / relpath
    path.write_bytes(path.read_bytes().replace(b"status: active", f"status: {value}".encode(), 1))


def set_property(root, relpath, key, value):
    path = root / relpath
    old = f"{key}: acme-bakery".encode()
    assert old in path.read_bytes()
    path.write_bytes(path.read_bytes().replace(old, f"{key}: {value}".encode(), 1))


def race_after_resolution(monkeypatch, change):
    """Run ``change`` between id resolution and the handler's own read."""
    from engagements import tools

    original = tools._resolve_targets

    def racing(*args, **kwargs):
        found = original(*args, **kwargs)
        change()
        return found

    monkeypatch.setattr(tools, "_resolve_targets", racing)


def race_after_bound_target(monkeypatch, change):
    from engagements import tools

    original = tools._bound_target

    def racing(*args, **kwargs):
        found = original(*args, **kwargs)
        change()
        return found

    monkeypatch.setattr(tools, "_bound_target", racing)


def home_tree(tmp_path):
    home = tmp_path / "home"
    (home / "clients" / "acme" / "website-2026").mkdir(parents=True)
    return home


# --- bind: refuse drift instead of persisting it ------------------------------


def test_bind_refuses_a_client_that_became_former_after_resolution(tmp_path, monkeypatch):
    root = copy_library(tmp_path)
    session, state = make_session(root)
    race_after_resolution(monkeypatch, lambda: set_status(root, ACME, "former"))

    result = call("engagements_bind", {"client": "acme-bakery"}, session)

    assert result["error"]["code"] == "invalid_binding"
    assert result["error"]["details"] == {"reason": "record_inactive", "record": "acme-bakery"}
    assert state == {}


def test_bind_refuses_a_client_whose_id_changed_after_resolution(tmp_path, monkeypatch):
    root = copy_library(tmp_path)
    session, state = make_session(root)
    race_after_resolution(monkeypatch, lambda: (root / ACME).write_bytes(OTHER.encode()))

    result = call("engagements_bind", {"client": "acme-bakery"}, session)

    assert result["error"]["code"] == "invalid_binding"
    assert result["error"]["details"] == {"reason": "record_identity_changed", "record": "acme-bakery"}
    assert state == {}
    assert "cedar-hill-cafe" not in json.dumps(result)


def test_bind_refuses_a_project_that_changed_client_after_resolution(tmp_path, monkeypatch):
    root = copy_library(tmp_path)
    session, state = make_session(root)
    race_after_resolution(monkeypatch, lambda: set_property(root, PROJECT, "client", "riverbend-dental"))

    result = call("engagements_bind", {"client": "acme-bakery", "project": "acme-website-2026"}, session)

    assert result["error"]["code"] == "invalid_binding"
    assert result["error"]["details"] == {"reason": "client_changed", "record": "acme-website-2026"}
    assert state == {}


def test_bind_reports_the_bytes_it_read_not_the_discovery_row(tmp_path, monkeypatch):
    """A rename is not identity drift, so it binds; the response must be fresh."""
    root = copy_library(tmp_path)
    session, state = make_session(root)
    race_after_resolution(monkeypatch, lambda: (root / ACME).write_bytes(
        (root / ACME).read_bytes().replace(b"name: Acme Bakery", b"name: Acme Bakery Group", 1)))

    result = call("engagements_bind", {"client": "acme-bakery"}, session)

    assert result["ok"], result
    from tools_support import rev_of
    assert result["data"]["client"]["name"] == "Acme Bakery Group"
    assert result["data"]["client"]["rev"] == rev_of(root / ACME)
    assert state, "the binding was persisted"


def test_auto_bound_context_refuses_a_project_that_became_historical(tmp_path, monkeypatch):
    """Auto mode's remembered match is revalidated against the bytes it reads."""
    root = copy_library(tmp_path)
    home = home_tree(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    session, state = make_session(root, cwd=str(home / "clients" / "acme" / "website-2026"))
    first = call("engagements_bind", {"mode": "auto"}, session)
    assert first["ok"] and first["data"]["state"] == "bound", first
    race_after_bound_target(monkeypatch, lambda: set_status(root, PROJECT, "historical"))

    result = call("engagements_context", {"kind": "project"}, session)

    assert result["error"]["code"] == "unbound"
    assert result["error"]["details"]["state"] == "invalid_binding"
    assert result["error"]["details"]["reason"] == "record_inactive"


# --- bound context: same bytes, same decision --------------------------------


def bind_client(session):
    result = call("engagements_bind", {"client": "acme-bakery"}, session)
    assert result["ok"], result


def bind_project(session):
    result = call("engagements_bind", {"client": "acme-bakery", "project": "acme-website-2026"}, session)
    assert result["ok"], result


def test_bound_context_refuses_a_client_that_became_former(tmp_path):
    root = copy_library(tmp_path)
    session, _ = make_session(root)
    bind_client(session)
    set_status(root, ACME, "former")

    result = call("engagements_context", {}, session)

    assert result["error"]["code"] == "unbound"
    assert result["error"]["details"]["state"] == "invalid_binding"
    assert result["error"]["details"]["reason"] == "record_inactive"


def test_bound_context_refuses_a_client_whose_id_changed(tmp_path, monkeypatch):
    root = copy_library(tmp_path)
    session, _ = make_session(root)
    bind_client(session)
    # Replace the bytes between the binding's resolution and the read: discovery
    # still resolves acme-bakery, the file no longer carries it.
    race_after_bound_target(monkeypatch, lambda: (root / ACME).write_bytes(OTHER.encode()))

    result = call("engagements_context", {}, session)

    assert result["error"]["code"] == "unbound"
    assert result["error"]["details"]["state"] == "invalid_binding"
    assert result["error"]["details"]["reason"] == "record_identity_changed"
    assert "cedar-hill-cafe" not in json.dumps(result)


def test_bound_context_reports_a_client_that_is_gone_from_the_library(tmp_path):
    """Replacing the id outside the race window leaves no row: the id is missing."""
    root = copy_library(tmp_path)
    session, _ = make_session(root)
    bind_client(session)
    (root / ACME).write_bytes(OTHER.encode())

    result = call("engagements_context", {}, session)

    assert result["error"]["code"] == "unbound"
    assert result["error"]["details"]["reason"] == "record_missing"


def test_bound_context_refuses_a_project_that_changed_client(tmp_path):
    root = copy_library(tmp_path)
    session, _ = make_session(root)
    bind_project(session)
    set_property(root, PROJECT, "client", "riverbend-dental")

    result = call("engagements_context", {"kind": "project"}, session)

    assert result["error"]["code"] == "unbound"
    assert result["error"]["details"]["reason"] == "client_changed"


def test_bound_context_reports_a_record_that_vanished_before_the_read(tmp_path, monkeypatch):
    root = copy_library(tmp_path)
    session, _ = make_session(root)
    bind_client(session)
    race_after_bound_target(monkeypatch, lambda: (root / ACME).unlink())

    result = call("engagements_context", {}, session)

    assert result["error"]["code"] == "unbound"
    assert result["error"]["details"]["state"] == "invalid_binding"
    assert result["error"]["details"]["reason"] == "record_missing"


# --- the resolved pair is validated as a pair ---------------------------------

RIVER = "Riverbend Dental/Client.md"
EXTRA_PROJECT = """---
engagements: project
id: acme-brand-assets
client: acme-bakery
name: Brand Assets
status: active
locations: []
updated: 2026-11-05
---

## Purpose
Brand files.
"""


def race_after_authoritative(monkeypatch, change):
    """Run ``change`` between resolution and the authoritative reads."""
    from engagements import tools

    original = tools._read_authoritative

    def racing(*args, **kwargs):
        change()
        return original(*args, **kwargs)

    monkeypatch.setattr(tools, "_read_authoritative", racing)


def auto_session(tmp_path, monkeypatch):
    root = copy_library(tmp_path)
    home = home_tree(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    session, state = make_session(root, cwd=str(home / "clients" / "acme" / "website-2026"))
    return root, session, state


def watched(session, state):
    """Record every state write and snapshot the store before the call."""
    before = dict(state)
    writes = []
    original = session.state_set

    def state_set(key, value):
        writes.append((key, value))
        original(key, value)

    session.state_set = state_set
    return before, writes


def test_auto_bind_refuses_a_project_reparented_after_resolution(tmp_path, monkeypatch):
    root, session, state = auto_session(tmp_path, monkeypatch)
    race_after_authoritative(monkeypatch, lambda: set_property(root, PROJECT, "client", "riverbend-dental"))

    result = call("engagements_bind", {"mode": "auto"}, session)

    assert result["error"]["code"] == "invalid_binding", result
    assert result["error"]["details"] == {"reason": "client_changed", "record": "acme-website-2026"}
    assert state == {}


def test_auto_bind_refuses_a_project_reparented_to_an_inactive_client(tmp_path, monkeypatch):
    root, session, state = auto_session(tmp_path, monkeypatch)

    def change():
        set_property(root, PROJECT, "client", "riverbend-dental")
        set_status(root, RIVER, "former")

    race_after_authoritative(monkeypatch, change)

    result = call("engagements_bind", {"mode": "auto"}, session)

    assert result["error"]["code"] == "invalid_binding", result
    assert result["error"]["details"]["reason"] == "client_changed"
    assert state == {}


def test_auto_bind_refuses_a_project_made_historical_after_resolution(tmp_path, monkeypatch):
    root, session, state = auto_session(tmp_path, monkeypatch)
    race_after_authoritative(monkeypatch, lambda: set_status(root, PROJECT, "historical"))

    result = call("engagements_bind", {"mode": "auto"}, session)

    assert result["error"]["code"] == "invalid_binding", result
    assert result["error"]["details"] == {"reason": "record_inactive", "record": "acme-website-2026"}
    assert state == {}


def test_bound_project_read_refuses_a_client_made_former_after_resolution(tmp_path, monkeypatch):
    root = copy_library(tmp_path)
    session, state = make_session(root)
    bind_project(session)
    race_after_bound_target(monkeypatch, lambda: set_status(root, ACME, "former"))
    before, writes = watched(session, state)

    result = call("engagements_context", {"kind": "project", "sections": []}, session)

    assert result["error"]["code"] == "unbound", result
    assert result["error"]["details"]["state"] == "invalid_binding"
    assert result["error"]["details"]["reason"] == "record_inactive"
    assert writes == [] and state == before


def test_bound_client_read_refuses_a_project_made_historical_after_resolution(tmp_path, monkeypatch):
    root = copy_library(tmp_path)
    session, state = make_session(root)
    bind_project(session)
    race_after_bound_target(monkeypatch, lambda: set_status(root, PROJECT, "historical"))
    before, writes = watched(session, state)

    result = call("engagements_context", {"kind": "client", "sections": []}, session)

    assert result["error"]["code"] == "unbound", result
    assert result["error"]["details"]["state"] == "invalid_binding"
    assert result["error"]["details"]["reason"] == "record_inactive"
    assert writes == [] and state == before


def test_bound_client_read_refuses_a_reparented_project(tmp_path, monkeypatch):
    root = copy_library(tmp_path)
    session, state = make_session(root)
    bind_project(session)
    race_after_bound_target(monkeypatch, lambda: set_property(root, PROJECT, "client", "riverbend-dental"))
    before, writes = watched(session, state)

    result = call("engagements_context", {"kind": "client", "sections": []}, session)

    assert result["error"]["code"] == "unbound", result
    assert result["error"]["details"]["state"] == "invalid_binding"
    assert result["error"]["details"]["reason"] == "client_changed"
    assert writes == [] and state == before


def test_a_bound_client_read_does_not_own_a_project_reparented_before_the_read(tmp_path, monkeypatch):
    """Related rows come from the bytes too, not from the discovery row."""
    root = copy_library(tmp_path)
    (root / "Acme Bakery" / "Projects" / "Brand Assets.md").write_text(EXTRA_PROJECT, encoding="utf-8")
    session, _ = make_session(root)
    bind_client(session)
    race_after_bound_target(monkeypatch, lambda: set_property(root, "Acme Bakery/Projects/Brand Assets.md",
                                                             "client", "riverbend-dental"))

    result = call("engagements_context", {"sections": []}, session)

    assert result["ok"] is True, result
    listed = [project["id"] for project in result["data"]["projects"]]
    assert "acme-website-2026" in listed
    assert "acme-brand-assets" not in listed


# --- related project rows come from checked bytes -----------------------------
#
# The selected pair was validated, but the lists around it were not:
# a client's ``projects`` fell back to the discovery row when a project could not
# be read, and bind's ``active_projects`` came straight from discovery. A project
# that is gone, unreadable, or now names another client is not listed.

WEBSITE = "acme-website-2026"


def race_after_read_target(monkeypatch, change):
    from engagements import tools

    original = tools._read_target

    def racing(*args, **kwargs):
        found = original(*args, **kwargs)
        change()
        return found

    monkeypatch.setattr(tools, "_read_target", racing)


def project_ids(rows):
    return [row["id"] for row in rows]


def test_client_context_does_not_list_a_project_deleted_after_discovery(tmp_path, monkeypatch):
    root = copy_library(tmp_path)
    session, _ = make_session(root)
    race_after_read_target(monkeypatch, lambda: (root / PROJECT).unlink())

    result = call("engagements_context", {"id": "acme-bakery"}, session)

    assert result["ok"], result
    assert WEBSITE not in project_ids(result["data"]["projects"])


def test_bound_client_context_does_not_list_a_project_deleted_after_discovery(tmp_path, monkeypatch):
    root = copy_library(tmp_path)
    session, state = make_session(root)
    assert call("engagements_bind", {"client": "acme-bakery"}, session)["ok"]
    race_after_read_target(monkeypatch, lambda: (root / PROJECT).unlink())

    result = call("engagements_context", {}, session)

    assert result["ok"], result
    assert WEBSITE not in project_ids(result["data"]["projects"])


def test_client_context_still_lists_a_readable_project(tmp_path):
    root = copy_library(tmp_path)
    session, _ = make_session(root)

    result = call("engagements_context", {"id": "acme-bakery"}, session)

    assert WEBSITE in project_ids(result["data"]["projects"])


def test_bind_active_projects_drops_a_project_reparented_after_resolution(tmp_path, monkeypatch):
    root = copy_library(tmp_path)
    session, state = make_session(root)
    race_after_resolution(monkeypatch, lambda: set_property(root, PROJECT, "client", "riverbend-dental"))

    result = call("engagements_bind", {"client": "acme-bakery"}, session)

    assert result["ok"], result
    assert WEBSITE not in project_ids(result["data"]["active_projects"])
    assert state, "a client-only binding is still valid and persisted"


def test_bind_active_projects_drops_a_project_deleted_after_resolution(tmp_path, monkeypatch):
    root = copy_library(tmp_path)
    session, state = make_session(root)
    race_after_resolution(monkeypatch, lambda: (root / PROJECT).unlink())

    result = call("engagements_bind", {"client": "acme-bakery"}, session)

    assert result["ok"], result
    assert WEBSITE not in project_ids(result["data"]["active_projects"])


def test_bind_active_projects_drops_a_project_made_historical_after_resolution(tmp_path, monkeypatch):
    root = copy_library(tmp_path)
    session, _ = make_session(root)
    race_after_resolution(monkeypatch, lambda: set_status(root, PROJECT, "historical"))

    result = call("engagements_bind", {"client": "acme-bakery"}, session)

    assert result["ok"], result
    assert WEBSITE not in project_ids(result["data"]["active_projects"])


def test_bind_active_projects_lists_a_readable_active_project(tmp_path):
    root = copy_library(tmp_path)
    session, _ = make_session(root)

    result = call("engagements_bind", {"client": "acme-bakery"}, session)

    assert WEBSITE in project_ids(result["data"]["active_projects"])
