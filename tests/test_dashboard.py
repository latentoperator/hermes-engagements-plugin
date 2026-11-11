"""The read-only dashboard (SPEC 6.7): its data views, its backend routes, the change notice and
the two front ends' read-only contract. Fictional data only."""

import importlib.util
import json
import re
import sys
import types
from pathlib import Path

import pytest
from engagements import dashboard_data, runtime
from engagements.dashboard_data import ReadOnlyState
from engagements.library import open_library_readonly
from plugin_support import ROOT, project_home, registered
from tools_support import copy_library, library_id


class Recorder:
    """A host state store that records every write, and a change log."""

    def __init__(self):
        self.values = {}
        self.writes = []
        self.changes = []


def make_host(settings, store=None, *, changed=None, default_root=None):
    store = store or Recorder()

    def get_setting(key, default=None):
        return settings.get(key, default)

    return runtime.Host(
        get_setting=get_setting,
        default_root=default_root or (lambda: Path("/nonexistent-engagements-default")),
        state_get=lambda key: store.values.get(runtime.state_key(key)),
        state_set=dashboard_data.refuse_state_set,
        session_cwd=lambda: None,
        session_surface=lambda: None,
        profile=lambda: None,
        clock=lambda: None,
        changed=changed,
    ), store


# --- overview -------------------------------------------------------------------------------------


def test_overview_lists_clients_with_their_projects_and_folder_status(monkeypatch, tmp_path):
    root = copy_library(tmp_path)
    home = project_home(monkeypatch, tmp_path)
    host, _ = make_host({"library_root": str(root)})
    data = dashboard_data.overview(host)
    assert data["status"] == "ok"
    assert data["library_root"] == str(root)
    order = [(c["status"] != "active", c["name"].casefold()) for c in data["clients"]]
    assert order == sorted(order), "active clients first, then by name"
    assert {"acme-bakery", "riverbend-dental"} <= {c["id"] for c in data["clients"]}
    projects = {p["id"]: p for c in data["clients"] for p in c["projects"]}
    assert data["counts"]["projects"] == len(projects) + len(data["unassigned"])
    for client in data["clients"]:
        assert all(p["client"] == client["id"] for p in client["projects"])
        assert client["abs_path"].startswith(str(root)) and client["abs_path"].lower().endswith("client.md")
    located = [loc for p in projects.values() for loc in p["locations"]]
    assert located, "the studio fixture declares project folders"
    for loc in located:
        assert loc["found"] == Path(loc["expanded"]).is_dir()
        if loc["path"].startswith("~"):
            assert loc["expanded"].startswith(str(home))
    assert data["doctor"]["clean"] in (True, False)
    assert set(data["doctor"]["counts"]) >= {"error", "warning"}


def test_only_local_paths_are_folders_and_urls_are_references(monkeypatch, tmp_path):
    """SPEC 4: URLs and other non-local forms are references, so the views must not call one a
    missing folder (P3V found the Acme site URL badged "not found here")."""
    root = copy_library(tmp_path)
    project_home(monkeypatch, tmp_path)
    host, _ = make_host({"library_root": str(root)})
    projects = {p["id"]: p for c in dashboard_data.overview(host)["clients"] for p in c["projects"]}
    by_path = {loc["path"]: loc for loc in projects["acme-website-2026"]["locations"]}
    url = by_path["https://git.example/studio/acme-site"]
    assert url["local"] is False and url["found"] is False
    assert by_path["~/clients/acme/website-2026"]["local"] is True
    record = dashboard_data.record(host, "acme-website-2026")
    assert record is not None
    assert {loc["path"]: loc["local"] for loc in record["properties"]["locations"]} == {
        path: loc["local"] for path, loc in by_path.items()}
    assert dashboard_data._location("relative/folder")["local"] is False
    assert dashboard_data._location("//server/share")["local"] is False


def test_overview_reports_folders_that_are_not_on_this_machine(tmp_path, monkeypatch):
    root = copy_library(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    host, _ = make_host({"library_root": str(root)})
    located = [loc for c in dashboard_data.overview(host)["clients"] for p in c["projects"] for loc in p["locations"]]
    assert located and not any(loc["found"] for loc in located)


def test_a_missing_library_is_explained_and_never_created(tmp_path):
    root = tmp_path / "not-yet"
    host, _ = make_host({"library_root": str(root)})
    data = dashboard_data.overview(host)
    assert data["status"] == "missing" and data["clients"] == []
    assert "No library exists at" in data["message"] and str(root) in data["message"]
    assert not root.exists()


def test_the_default_library_is_not_created_by_looking_at_it(tmp_path):
    default = tmp_path / "hermes-home" / "plugin-data" / "engagements" / "library"
    host, _ = make_host({}, default_root=lambda: default)
    data = dashboard_data.overview(host)
    assert data["status"] == "missing"
    assert "the first time the agent uses a client tool" in data["message"]
    assert not default.exists() and not (tmp_path / "hermes-home").exists()


def test_a_relative_library_root_is_unavailable_with_the_settings_message(tmp_path):
    host, _ = make_host({"library_root": "relative/path"})
    data = dashboard_data.overview(host)
    assert data["status"] in ("unavailable", "missing")
    assert data["message"]


# --- one record ---------------------------------------------------------------------------------


def test_record_returns_properties_and_sections_without_writing(tmp_path, monkeypatch):
    root = copy_library(tmp_path)
    project_home(monkeypatch, tmp_path)
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    host, store = make_host({"library_root": str(root)})
    client = dashboard_data.record(host, "riverbend-dental")
    assert client["kind"] == "client" and client["name"] == "Riverbend Dental"
    assert client["sections"] and all({"title", "text"} <= set(s) for s in client["sections"])
    assert client["truncated"] is False
    assert all(p["id"] for p in client["projects"])
    project_id = client["projects"][0]["id"]
    project = dashboard_data.record(host, project_id)
    assert project["kind"] == "project" and project["client"]["id"] == "riverbend-dental"
    assert project["client"]["name"] == "Riverbend Dental"
    assert isinstance(project["properties"]["locations"], list)
    after = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert after == before
    assert store.writes == []


def test_record_of_an_unknown_id_is_none(tmp_path):
    host, _ = make_host({"library_root": str(copy_library(tmp_path))})
    assert dashboard_data.record(host, "no-such-client") is None


def test_a_long_record_is_truncated_and_says_so(tmp_path, monkeypatch):
    root = copy_library(tmp_path)
    monkeypatch.setattr(dashboard_data, "BODY_LIMIT", 40)
    host, _ = make_host({"library_root": str(root)})
    data = dashboard_data.record(host, "riverbend-dental")
    assert data["truncated"] is True
    assert sum(len(s["text"]) for s in data["sections"]) <= 40


# --- one id, two records --------------------------------------------------------------------------

TWO_RECORDS = """---
engagements: {kind}
id: {record_id}
{client}name: {name}
status: active
updated: 2026-11-05
---

# {name}

## Summary
"""


def add_record(root, relative, *, kind, record_id, name, client=None):
    """A second record beside the fixture's, laid out the way the library writes one."""
    target = Path(root) / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(TWO_RECORDS.format(kind=kind, record_id=record_id, name=name,
                                         client=f"client: {client}\n" if client else ""), encoding="utf-8")
    return target


def add_duplicate_client(root, record_id="acme-bakery", name="Zebra Duplicate"):
    return add_record(root, f"{name}/Client.md", kind="client", record_id=record_id, name=name)


def add_raw(root, relative, frontmatter):
    """A record written byte for byte, for the defects a template cannot express."""
    target = Path(root) / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"---\n{frontmatter}---\n\n## Summary\nFictional body.\n", encoding="utf-8")
    return target


def add_unreadable_kind(root, relative, record_id, name="Repeated Marker"):
    """A record whose ``engagements`` key repeats, so its id is readable and its kind is not."""
    return add_raw(root, relative, f"engagements: client\nengagements: project\nid: {record_id}\n"
                                   f"name: {name}\nstatus: active\nupdated: 2026-11-05\n")


def add_unreadable_id(root, relative, record_id, name="Repeated Id"):
    """A client whose ``id`` key repeats, so the file has no id at all."""
    return add_raw(root, relative, f"engagements: client\nid: {record_id}\nid: {record_id}-again\n"
                                   f"name: {name}\nstatus: active\nupdated: 2026-11-05\n")


def rows(data):
    """Every record the page offers as a row: clients, their projects, and the unassigned ones."""
    listed = list(data["clients"])
    listed += [project for client in data["clients"] for project in client["projects"]]
    return listed + list(data["unassigned"])


def test_every_row_the_page_lists_opens_as_the_same_record(tmp_path):
    """The list shows one record per id and the detail view opens one by id.

    Two client files under one id made the list show the later file and the detail view the
    earlier one, so a row opened a record it had never listed.
    """
    root = copy_library(tmp_path)
    add_duplicate_client(root)
    host, _ = make_host({"library_root": str(root)})
    data = dashboard_data.overview(host)
    assert data["clients"], "control: the library still lists its other clients"
    for row in rows(data):
        opened = dashboard_data.record(host, row["id"])
        assert opened is not None, row["id"]
        assert (opened["name"], opened["path"]) == (row["name"], row["path"]), row["id"]


@pytest.mark.parametrize("shape,kinds", [
    ("two-clients", {"client"}),
    ("two-projects", {"project"}),
    ("client-and-project", {"client", "project"}),
])
def test_a_record_whose_id_another_record_uses_is_never_a_row(shape, kinds, tmp_path):
    """Two clients, two projects, and a real client with a real project: none of them is a row."""
    root = copy_library(tmp_path)
    shared = {"two-clients": "acme-bakery", "two-projects": "acme-website-2026",
              "client-and-project": "shared-client-project"}[shape]
    if shape == "two-clients":
        add_duplicate_client(root, record_id=shared)
    elif shape == "two-projects":
        add_record(root, "Acme Bakery/Projects/Website Copy.md", kind="project", record_id=shared,
                   name="Website 2026 Copy", client="acme-bakery")
    else:
        add_duplicate_client(root, record_id=shared, name="Shared Client")
        add_record(root, "Shared Client/Project.md", kind="project", record_id=shared,
                   name="Shared Project", client="riverbend-dental")
    host, _ = make_host({"library_root": str(root)})
    data = dashboard_data.overview(host)

    # What the fixture really holds for this id, so the case cannot drift away from its name.
    discovered = {entry.kind for entry in open_library_readonly(root).discover().entries if entry.id == shared}
    assert discovered == kinds
    assert shared not in [row["id"] for row in rows(data)]
    assert len([item for item in data["duplicated"] if item["id"] == shared]) == 2
    assert dashboard_data.record(host, shared)["status"] == "conflict"


def test_the_overview_reports_every_record_it_does_not_list(tmp_path):
    """Each unlisted record is named by path with the defects that kept it off the list."""
    root = copy_library(tmp_path)
    add_duplicate_client(root)
    host, _ = make_host({"library_root": str(root)})
    data = dashboard_data.overview(host)

    assert {(item["path"], tuple(item["defects"])) for item in data["duplicated"]} == {
        ("Acme Bakery/Client.md", ("duplicate_id",)), ("Zebra Duplicate/Client.md", ("duplicate_id",))}
    assert data["unreadable"] == [], "a duplicated id is its own list, not an unreadable record"
    assert data["counts"]["clients"] == len(data["clients"]) == 2
    listed_projects = sum(len(client["projects"]) for client in data["clients"]) + len(data["unassigned"])
    assert data["counts"]["projects"] == listed_projects


def test_the_notice_names_every_record_it_does_not_list_exactly_once(tmp_path):
    """Every discovery entry is a row or named once in the notice, whatever else is wrong with it.

    A record carrying a duplicated id is named with that reason even when its kind cannot be
    read, and a record with no readable id or kind is named too: a page that lists records and
    opens them by id must never leave one of them out silently.
    """
    root = copy_library(tmp_path)
    add_duplicate_client(root)                                        # duplicate
    add_unreadable_kind(root, "Broken/Repeated.md", "acme-bakery")    # duplicate, unreadable kind
    add_unreadable_id(root, "Broken/NoId.md", "orphan")               # no readable id
    add_unreadable_kind(root, "Broken/Lonely.md", "broken-only")      # unreadable kind, own id
    host, _ = make_host({"library_root": str(root)})
    data = dashboard_data.overview(host)
    entries = open_library_readonly(root).discover().entries

    listed = {row["path"] for row in rows(data)}
    named = [item["path"] for item in data["unlisted"]]
    assert len(named) == len(set(named)), "no record is named twice"
    assert not listed & set(named), "a record is either a row or named, never both"
    assert listed | set(named) == {entry.relpath for entry in entries}
    assert {item["path"]: item["reason"] for item in data["unlisted"]} == {
        "Acme Bakery/Client.md": "Its id is used by more than one record.",
        "Zebra Duplicate/Client.md": "Its id is used by more than one record.",
        "Broken/Repeated.md": "Its id is used by more than one record.",
        "Broken/NoId.md": "Its id could not be read.",
        "Broken/Lonely.md": "Its kind could not be read as a client or project.",
    }
    assert [item["path"] for item in data["unlisted"]] == sorted(named), "named in path order"
    assert [item["path"] for item in data["duplicated"]] == ["Acme Bakery/Client.md", "Broken/Repeated.md",
                                                            "Zebra Duplicate/Client.md"]
    assert [item["path"] for item in data["unreadable"]] == ["Broken/Lonely.md", "Broken/NoId.md"]


def test_a_project_whose_client_id_is_shared_is_unassigned(tmp_path):
    """Neither duplicate owns the project: it is listed as needing a client, like an unknown id."""
    root = copy_library(tmp_path)
    add_duplicate_client(root)
    host, _ = make_host({"library_root": str(root)})
    data = dashboard_data.overview(host)

    assert "acme-website-2026" in [project["id"] for project in data["unassigned"]]
    assert not any(project["id"] == "acme-website-2026" for client in data["clients"] for project in client["projects"])
    project = dashboard_data.record(host, "acme-website-2026")
    assert project["client"] == {"id": "acme-bakery", "name": None, "known": False}


def test_a_client_never_lists_a_project_the_page_cannot_open(tmp_path):
    """A project under a duplicated id is not in the list, so it is not in its client's either."""
    root = copy_library(tmp_path)
    add_record(root, "Acme Bakery/Projects/Website Copy.md", kind="project", record_id="acme-website-2026",
               name="Website 2026 Copy", client="acme-bakery")
    host, _ = make_host({"library_root": str(root)})
    client = dashboard_data.record(host, "acme-bakery")
    assert client["status"] == "ok" and client["id"] == "acme-bakery"
    assert "acme-website-2026" not in [project["id"] for project in client["projects"]]


def test_a_duplicated_id_returns_a_conflict_and_no_record(tmp_path):
    """A shared id names neither record, so the view answers neither of them with a body."""
    root = copy_library(tmp_path)
    add_duplicate_client(root)
    host, _ = make_host({"library_root": str(root)})

    data = dashboard_data.record(host, "acme-bakery")
    assert data["status"] == "conflict"
    assert "used by more than one record" in data["message"]
    assert not {"id", "kind", "name", "path", "sections", "properties", "projects"} & set(data)
    assert dashboard_data.record(host, "riverbend-dental")["status"] == "ok", "control: a unique id still opens"


def test_an_id_shared_only_by_unreadable_kinds_is_a_conflict(tmp_path, monkeypatch):
    """Three files with one id and no readable kind still share the id, so it is not a 404.

    Duplication is decided over every discovery entry carrying the id, before the kind filter:
    the id exists more than once, so the page owes the conflict rather than "no such record".
    """
    root = copy_library(tmp_path)
    for number in range(3):
        add_unreadable_kind(root, f"Broken/record_{number}.md", "shared-id")
    host, _ = make_host({"library_root": str(root)})

    def no_body(*args):
        pytest.fail("a body was read for an id used by more than one record")

    monkeypatch.setattr(dashboard_data, "_read_plain", no_body)
    data = dashboard_data.record(host, "shared-id")
    assert data is not None, "a shared id is not absent"
    assert data["status"] == "conflict"
    assert not {"id", "kind", "name", "path", "sections", "properties"} & set(data)


def test_an_id_shared_by_a_readable_and_an_unreadable_record_is_a_conflict(tmp_path):
    """One client and one file with an unreadable kind under the same id: still no single record."""
    root = copy_library(tmp_path)
    add_unreadable_kind(root, "Broken/Repeated.md", "acme-bakery")
    host, _ = make_host({"library_root": str(root)})

    data = dashboard_data.record(host, "acme-bakery")
    assert data is not None, "a shared id is not absent"
    assert data["status"] == "conflict"
    assert "used by more than one record" in data["message"]
    assert not {"id", "kind", "name", "path", "sections"} & set(data)


def test_a_single_record_that_is_not_a_client_or_project_is_still_not_found(tmp_path, monkeypatch):
    """An id no entry has, and an id on one file whose kind cannot be read, both stay absent."""
    root = copy_library(tmp_path)
    add_unreadable_kind(root, "Broken/Lonely.md", "broken-only")
    host, _ = make_host({"library_root": str(root)})

    assert dashboard_data.record(host, "broken-only") is None
    assert dashboard_data.record(host, "no-such-client") is None
    api, _, writes = load_api(monkeypatch, tmp_path, {"library_root": str(root)})
    for record_id in ("broken-only", "no-such-client"):
        with pytest.raises(_HTTPException) as missing:
            api.record(record_id)
        assert missing.value.status_code == 404, record_id
    assert writes == []


def test_an_id_shared_only_by_unreadable_kinds_is_a_conflict_on_the_route(tmp_path, monkeypatch):
    """The route answers 409 for such an id, and never a record body."""
    root = copy_library(tmp_path)
    for number in range(2):
        add_unreadable_kind(root, f"Broken/record_{number}.md", "shared-id")
    api, _, writes = load_api(monkeypatch, tmp_path, {"library_root": str(root)})
    core = api._core("dashboard_data")

    def no_body(*args):
        pytest.fail("a body was read for an id used by more than one record")

    monkeypatch.setattr(core, "_read_plain", no_body)
    with pytest.raises(_HTTPException) as conflict:
        api.record("shared-id")
    assert conflict.value.status_code == 409
    assert "used by more than one record" in conflict.value.detail
    assert writes == []


def test_the_session_view_never_names_one_of_two_records_under_an_id(tmp_path):
    """An automatic binding that names a shared id resolves to no client, not one duplicate's name."""
    root = copy_library(tmp_path)
    add_duplicate_client(root)
    store = Recorder()
    host, _ = make_host({"library_root": str(root)}, store)
    key = runtime.state_key(f"binding:{library_id(root)}:s-dash")
    store.values[key] = {"mode": "auto", "client": "acme-bakery", "project": "acme-website-2026"}
    data = dashboard_data.session_view(host, "s-dash")
    assert data["state"] == "invalid_binding"
    assert data["client"] is None and data["project"] is None
    store.values[key] = {"mode": "auto", "client": "riverbend-dental", "project": "riverbend-brand-assets"}
    assert dashboard_data.session_view(host, "s-dash")["client"]["id"] == "riverbend-dental", "control"


# --- bindings, read-only ------------------------------------------------------------------------


def test_session_view_shows_the_binding_the_agent_made(monkeypatch, tmp_path):
    root = copy_library(tmp_path)
    _, ctx = registered(monkeypatch, tmp_path, library=root)
    assert ctx.call_tool("engagements_bind", {"client": "riverbend-dental"}, session_id="s-dash")["ok"]
    store = Recorder()
    store.values = ctx.state.values
    host, _ = make_host({"library_root": str(root)}, store)
    data = dashboard_data.session_view(host, "s-dash")
    assert data["state"] == "bound"
    assert data["client"] == {"id": "riverbend-dental", "name": "Riverbend Dental"}
    assert data["automatic"] is False
    unbound = dashboard_data.session_view(host, "s-other")
    assert unbound["client"] is None and unbound["state"] != "bound"
    assert dashboard_data.session_view(host, None)["session"] is False


def test_the_dashboard_host_refuses_to_write_state():
    with pytest.raises(ReadOnlyState):
        dashboard_data.refuse_state_set("k", 1)


def test_doctor_view_returns_the_report(tmp_path):
    host, _ = make_host({"library_root": str(copy_library(tmp_path))})
    data = dashboard_data.doctor_view(host)
    assert data["status"] == "ok" and "findings" in json.dumps(data["report"])


# --- change notices -----------------------------------------------------------------------------


def _notices(monkeypatch, tmp_path, root):
    seen = []
    from plugin_support import load_plugin

    monkeypatch.setattr(load_plugin(), "_changed", seen.append)
    _, ctx = registered(monkeypatch, tmp_path, library=root)
    return seen, ctx


def test_successful_writes_and_binds_notify_the_dashboard(monkeypatch, tmp_path):
    seen, ctx = _notices(monkeypatch, tmp_path, copy_library(tmp_path))
    assert ctx.call_tool("engagements_find", {"query": ""}, session_id="s1")["ok"]
    assert seen == []
    assert ctx.call_tool("engagements_create", {"kind": "client", "name": "Harbor Yoga"}, session_id="s1")["ok"]
    assert seen == ["library"]
    assert ctx.call_tool("engagements_bind", {"client": "harbor-yoga"}, session_id="s1")["ok"]
    assert seen == ["library", "binding"]


def test_a_failed_tool_call_sends_no_notice(monkeypatch, tmp_path):
    seen, ctx = _notices(monkeypatch, tmp_path, copy_library(tmp_path))
    assert ctx.call_tool("engagements_bind", {"client": "no-such-client"}, session_id="s1")["ok"] is False
    assert seen == []


def test_a_broken_notice_never_changes_the_tool_result(monkeypatch, tmp_path):
    from plugin_support import load_plugin

    def boom(what):
        raise RuntimeError("no gateway")

    monkeypatch.setattr(load_plugin(), "_changed", boom)
    _, ctx = registered(monkeypatch, tmp_path, library=copy_library(tmp_path))
    result = ctx.call_tool("engagements_create", {"kind": "client", "name": "Harbor Yoga"}, session_id="s1")
    assert result["ok"] is True and result["data"]["id"] == "harbor-yoga"


def test_the_real_notice_uses_the_public_event_api(monkeypatch):
    from plugin_support import load_plugin

    calls = []
    events = types.ModuleType("hermes_cli.plugin_events")
    events.broadcast_plugin_event = lambda *args: calls.append(args)
    package = types.ModuleType("hermes_cli")
    package.__path__ = []
    monkeypatch.setitem(sys.modules, "hermes_cli", package)
    monkeypatch.setitem(sys.modules, "hermes_cli.plugin_events", events)
    load_plugin()._changed("binding")
    assert calls == [("engagements", "changed", {"what": "binding"})]


# --- backend routes -----------------------------------------------------------------------------


class _HTTPException(Exception):
    def __init__(self, status_code, detail=None):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class _Router:
    def __init__(self):
        self.routes = {}

    def get(self, path):
        def decorate(fn):
            self.routes[path] = fn
            return fn

        return decorate

    def __getattr__(self, name):  # any other verb would be a write route
        raise AssertionError(f"plugin_api registered a {name.upper()} route; the dashboard is read-only")


def load_api(monkeypatch, tmp_path, settings, state=None):
    """``dashboard/plugin_api.py`` with stand-ins for FastAPI and the public Hermes modules it imports.

    ``state`` is written to the scratch plugin state file before the routes run, so the
    backend reads it the way it reads the host's file. The stand-in ``PluginState.get``
    takes the host's creating lock, as the real one does, so a route that goes through
    ``get`` leaves ``.state.json.lock`` behind and a route that reads ``path`` does not.
    """
    fastapi = types.ModuleType("fastapi")
    fastapi.APIRouter = _Router
    fastapi.HTTPException = _HTTPException
    monkeypatch.setitem(sys.modules, "fastapi", fastapi)

    hermes_cli = types.ModuleType("hermes_cli")
    hermes_cli.__path__ = []
    config = types.ModuleType("hermes_cli.config")
    config.load_config_readonly = lambda: {"plugins": {"entries": {"engagements": {"settings": settings}}}}
    state_mod = types.ModuleType("hermes_cli.plugins_state")
    writes = []
    home = tmp_path / "hermes-home"
    state_path = home / "plugin-data" / "engagements" / "state.json"

    class PluginState:
        def __init__(self, name):
            assert name == "engagements"
            self.path = state_path

        def get(self, key, default=None):
            lock = state_path.with_name(f".{state_path.name}.lock")
            lock.parent.mkdir(parents=True, exist_ok=True)
            with open(lock, "a+b"):
                pass
            try:
                values = json.loads(state_path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return default
            return values.get(key, default)

        def set(self, key, value):
            writes.append(key)

    state_mod.PluginState = PluginState
    constants = types.ModuleType("hermes_constants")
    constants.get_hermes_home = lambda: str(home)
    for name, module in (("hermes_cli", hermes_cli), ("hermes_cli.config", config),
                         ("hermes_cli.plugins_state", state_mod), ("hermes_constants", constants)):
        monkeypatch.setitem(sys.modules, name, module)
    if state is not None:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state), encoding="utf-8")
    for name in [m for m in sys.modules if m.startswith("hermes_dashboard_engagements_core")]:
        monkeypatch.delitem(sys.modules, name)
    spec = importlib.util.spec_from_file_location("engagements_plugin_api_test", ROOT / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, PluginState, writes


def test_backend_routes_are_get_only_and_serve_the_views(monkeypatch, tmp_path):
    root = copy_library(tmp_path)
    api, _, writes = load_api(monkeypatch, tmp_path, {"library_root": str(root)})
    assert set(api.router.routes) == {"/overview", "/record/{record_id}", "/session", "/doctor"}
    assert api.overview()["status"] == "ok"
    assert api.record("riverbend-dental")["name"] == "Riverbend Dental"
    with pytest.raises(_HTTPException) as missing:
        api.record("no-such-client")
    assert missing.value.status_code == 404
    assert api.session(id="")["session"] is False
    assert api.doctor()["status"] == "ok"
    assert writes == []


def test_an_id_two_records_use_is_a_conflict_on_the_route_and_never_a_body(monkeypatch, tmp_path):
    """A shared id is not a 404 (the id exists) and not a record (one of two would be wrong)."""
    root = copy_library(tmp_path)
    add_duplicate_client(root)
    api, _, writes = load_api(monkeypatch, tmp_path, {"library_root": str(root)})

    with pytest.raises(_HTTPException) as conflict:
        api.record("acme-bakery")
    assert conflict.value.status_code == 409
    assert "used by more than one record" in conflict.value.detail
    with pytest.raises(_HTTPException) as missing:
        api.record("no-such-client")
    assert missing.value.status_code == 404
    assert conflict.value.detail != missing.value.detail
    assert api.record("riverbend-dental")["name"] == "Riverbend Dental", "control: a unique id still opens"
    assert writes == []


def test_backend_reads_the_binding_from_plugin_state(monkeypatch, tmp_path):
    root = copy_library(tmp_path)
    _, ctx = registered(monkeypatch, tmp_path, library=root)
    assert ctx.call_tool("engagements_bind", {"client": "acme-bakery"}, session_id="s-web")["ok"]
    api, _, writes = load_api(monkeypatch, tmp_path, {"library_root": str(root)}, state=dict(ctx.state.values))
    data = api.session(id="s-web")
    assert data["client"]["id"] == "acme-bakery"
    assert writes == []
    assert not (tmp_path / "hermes-home" / "plugin-data" / "engagements" / ".state.json.lock").exists()


def test_backend_default_root_is_computed_without_creating_it(monkeypatch, tmp_path):
    api, _, _ = load_api(monkeypatch, tmp_path, {})
    data = api.overview()
    assert data["status"] == "missing"
    assert not (tmp_path / "hermes-home").exists()


def test_backend_hides_unexpected_errors_behind_a_sentence(monkeypatch, tmp_path):
    api, _, _ = load_api(monkeypatch, tmp_path, {"library_root": str(copy_library(tmp_path))})
    assert api.overview()["status"] == "ok"  # loads the core
    core = sys.modules["hermes_dashboard_engagements_core.dashboard_data"]
    monkeypatch.setattr(core, "overview", lambda host: 1 / 0)
    with pytest.raises(_HTTPException) as failed:
        api.overview()
    assert failed.value.status_code == 500 and "ZeroDivision" not in str(failed.value.detail)


def test_backend_loads_the_core_without_touching_sys_path(monkeypatch, tmp_path):
    before = list(sys.path)
    load_api(monkeypatch, tmp_path, {})
    assert sys.path == before


# --- the two front ends -------------------------------------------------------------------------


DESKTOP = (ROOT / "desktop" / "plugin.js").read_text(encoding="utf-8")
WEB = (ROOT / "dashboard" / "dist" / "index.js").read_text(encoding="utf-8")


def test_desktop_imports_only_what_desktop_can_resolve():
    specifiers = set(re.findall(r"from '([^']+)'", DESKTOP))
    assert specifiers == {"@hermes/plugin-sdk", "react", "react/jsx-runtime"}
    assert "export default" in DESKTOP and "id: 'engagements'" in DESKTOP


@pytest.mark.parametrize("source", [DESKTOP, WEB], ids=["desktop", "web"])
def test_front_ends_never_send_a_write_request(source):
    assert not re.search(r"method\s*:\s*['\"](POST|PUT|PATCH|DELETE)", source, re.IGNORECASE)
    assert "composer.submit" not in source  # a chat is started as a draft the user sends


@pytest.mark.parametrize("source", [DESKTOP, WEB], ids=["desktop", "web"])
def test_front_ends_keep_layout_out_of_classes_the_host_css_lacks(source):
    """Neither front end has a build step, so a utility class works only if the host's compiled CSS
    already carries it. P3V found every arbitrary-value class (``grid-cols-[...]``) and the web
    tab's ``pl-6`` missing, which collapsed both pages to one column. Layout lives in ``style``."""
    found = re.findall(r"className:\s*(?:`([^`]*)`|'([^']*)'|\"([^\"]*)\")", source)
    classes = " ".join(" ".join(groups) for groups in found)
    assert classes.strip(), "the class scan found nothing; the pattern no longer matches the source"
    assert not re.search(r"\b[\w:-]*-\[[^\]]*\]", classes), "arbitrary-value class in a front end"
    assert "pl-6" not in classes.split()
    assert "gridTemplateColumns" in source
    # A bare ``1fr`` track has an auto minimum, so one long path widens the grid past the window
    # (P3V on Desktop: the record pane ran 270px off-screen). Every fr track starts at zero.
    for columns in re.findall(r"gridTemplateColumns:\s*['\"]([^'\"]*)['\"]", source):
        assert not re.search(r"(?<!minmax\(0, )\b\d*fr\b", columns), columns


def test_desktop_record_pane_fits_its_column():
    """The SDK ScrollArea's Radix viewport wraps content in ``display: table``, which grows to the
    content's natural width: P3V measured the record pane 285px past a 937px window even with
    ``minmax(0, 1fr)`` columns. The page scrolls through a plain overflow box instead."""
    assert not re.search(r"jsx\(\s*ScrollArea\b", DESKTOP)
    assert not re.search(r"^\s*ScrollArea,\s*$", DESKTOP, re.M)
    # Row actions are icons: a labelled "Copy path" button outran a 900px window (P3V run 3).
    copies = re.findall(r"jsx\(CopyButton, \{[^}]*\}", DESKTOP)
    assert copies and all("appearance: 'icon'" in c for c in copies), copies


@pytest.mark.parametrize("source", [DESKTOP, WEB], ids=["desktop", "web"])
def test_front_ends_count_only_local_folders(source):
    """A URL location is a reference (SPEC 4), never a folder or a missing one."""
    assert re.search(r"locations\.filter\(\s*(?:function \(l\) \{ return l\.local; \}|l => l\.local)\s*\)", source)
    assert "Links and references" in source


def test_manifest_names_the_plugin_and_its_backend():
    manifest = json.loads((ROOT / "dashboard" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "engagements"
    assert manifest["api"] == "plugin_api.py" and manifest["entry"] == "dist/index.js"
    assert manifest["tab"]["path"] == "/engagements"
    assert "engagements" in WEB and "__HERMES_PLUGINS__.register(\"engagements\"" in WEB


def test_every_front_end_file_is_shipped_by_git():
    """A plugin installs from git, so an ignored front end would silently vanish (dist/ is ignored)."""
    import subprocess

    shipped = ["desktop/plugin.js", "dashboard/dist/index.js", "dashboard/manifest.json", "dashboard/plugin_api.py"]
    result = subprocess.run(["git", "check-ignore", *shipped], cwd=ROOT, capture_output=True, text=True, check=False)
    if result.returncode == 128:
        pytest.skip("not a git checkout")
    assert result.stdout.strip() == ""
