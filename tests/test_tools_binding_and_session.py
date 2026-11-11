"""engagements_bind and engagements_context: session binding, modes and state isolation."""

import copy
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from tools_support import (
    binding_key_for,
    call,
    copy_library,
    fixed_clock,
    library_id,
    make_session,
    stored_binding,
)


def _bound_at():
    return "2026-11-07T14:02:00-06:00"


def store(state, root, session_id, value):
    state[binding_key_for(root, session_id)] = value


def session_with_state(root, state_get, state_set, *, session_id="sess-1"):
    from engagements.tools import Session, SessionConfig

    return Session(session_id=session_id, cwd=None, surface="cli", profile="studio-dev",
                   config=SessionConfig(library_root=root), state_get=state_get, state_set=state_set,
                   clock=fixed_clock)


def unbound_details(response):
    assert response["ok"] is False, response
    error = response["error"]
    assert error["code"] == "unbound"
    assert error["message"] == ("No id or path was given and no client or project is bound to this session.")
    details = error["details"]
    assert set(details) == {"mode", "state", "reason", "evidence", "candidates", "auto"}
    assert set(details["auto"]) == {"state", "reason", "evidence", "candidates"}
    assert isinstance(details["candidates"], list)
    assert isinstance(details["auto"]["candidates"], list)
    return details


def home_tree(tmp_path):
    home = tmp_path / "home"
    for relative in ("clients/shared", "clients/acme/website-2026", "clients/riverbend/recall"):
        (home / relative).mkdir(parents=True)
    return home


# --- no_session and library_missing ------------------------------------------


def test_bind_without_a_session_id_is_no_session(tmp_path):
    root = copy_library(tmp_path)
    session, state = make_session(root, session_id=None)
    result = call("engagements_bind", {"client": "acme-bakery"}, session)
    assert result["ok"] is False
    assert result["error"]["code"] == "no_session"
    assert result["error"]["message"] == "This call has no session id, so there is nothing to bind to."
    assert result["error"]["details"] == {}
    assert state == {}


CORRUPT_BINDING_MESSAGE = ("The stored binding is unreadable. Nothing is bound; do not rely on client context from "
                           "earlier turns. Pick a client with engagements_bind.")


@pytest.mark.parametrize("value", (["bad"], "corrupt", 5, True, ["client", "project"]))
def test_a_stored_binding_that_is_not_a_mapping_reads_as_an_invalid_binding(tmp_path, value):
    root = copy_library(tmp_path)
    session, state = make_session(root)
    store(state, root, "sess-1", value)
    result = call("engagements_context", {}, session)
    assert result["ok"] is False, result
    error = result["error"]
    assert error["code"] == "unbound"
    assert error["message"] == CORRUPT_BINDING_MESSAGE
    details = error["details"]
    assert details["state"] == "invalid_binding" and details["reason"] == "stored_unreadable"
    assert details["candidates"] == []
    # details.auto is the live folder resolution, not a copy of the refusal this value caused
    assert details["auto"]["state"] == "unbound"
    assert details["auto"].get("reason") != "stored_unreadable"


def test_a_stored_binding_with_an_unusable_mode_is_an_invalid_binding_too(tmp_path):
    root = copy_library(tmp_path)
    session, state = make_session(root)
    store(state, root, "sess-1", {"mode": "unexpected"})
    error = call("engagements_context", {}, session)["error"]
    assert error["code"] == "unbound" and error["message"] == CORRUPT_BINDING_MESSAGE
    assert error["details"]["state"] == "invalid_binding"
    assert error["details"]["reason"] == "stored_unreadable"
    # auto is the live folder resolution, reported independently of the stored mode
    assert error["details"]["auto"]["state"] == "unbound"


# A stored value the plugin cannot read: not the object it writes, a mode it does not know, and
# an automatic identity with one half missing (spec 6.4 says ``details.auto`` is still the live
# automatic resolution, so none of these may stand in for it).
CORRUPT_STORED = (
    pytest.param(["bad"], id="list"),
    pytest.param("corrupt", id="string"),
    pytest.param(5, id="number"),
    pytest.param({}, id="empty-mapping"),
    pytest.param({"mode": None}, id="null-mode"),
    pytest.param({"mode": 7}, id="unknown-mode"),
    pytest.param({"mode": "auto", "client": "acme-bakery"}, id="truncated-auto-identity"),
)


def _auto_with_nothing_stored(root, cwd):
    """The resolution this session and folder give when no binding is stored at all.

    Only meaningful for a folder the empty store cannot bind (ambiguous or incomplete), where
    the call refuses and reports its resolution in ``details.auto`` like any other read.
    """
    control, _ = make_session(root, session_id="sess-control", cwd=cwd)
    result = call("engagements_context", {}, control)
    assert result["ok"] is False, result
    return result["error"]["details"]["auto"]


def _corrupt_details(root, cwd, value):
    """The details of the refusal a corrupt stored value causes, once it is in the store."""
    session, state = make_session(root, cwd=cwd)
    store(state, root, "sess-1", value)
    result = call("engagements_context", {}, session)
    assert result["ok"] is False, result
    error = result["error"]
    assert error["code"] == "unbound" and error["message"] == CORRUPT_BINDING_MESSAGE
    details = error["details"]
    assert (details["state"], details["reason"]) == ("invalid_binding", "stored_unreadable")
    assert details["candidates"] == []
    return details


@pytest.mark.parametrize("value", CORRUPT_STORED)
def test_a_corrupt_stored_binding_still_reports_the_folder_resolution_that_matches(tmp_path, monkeypatch, value):
    root = copy_library(tmp_path)
    home = home_tree(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    cwd = str(home / "clients" / "acme" / "website-2026")
    # with nothing stored this folder binds acme-website-2026; the corrupt value may refuse the
    # read, but it cannot make the folder match look like a failure of the folder
    control, _ = make_session(root, session_id="sess-control", cwd=cwd)
    read = call("engagements_context", {"kind": "project"}, control)
    assert read["ok"] and read["data"]["id"] == "acme-website-2026", read
    details = _corrupt_details(root, cwd, value)
    assert (details["auto"]["state"], details["auto"]["reason"]) == ("bound", None)
    assert "acme-website-2026" in details["auto"]["evidence"]


@pytest.mark.parametrize("value", CORRUPT_STORED)
def test_a_corrupt_stored_binding_still_reports_an_ambiguous_folder_resolution(tmp_path, monkeypatch, value):
    root = copy_library(tmp_path)
    home = home_tree(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    cwd = str(home / "clients" / "shared")
    details = _corrupt_details(root, cwd, value)
    assert details["auto"] == _auto_with_nothing_stored(root, cwd)
    assert details["auto"]["state"] == "ambiguous"
    assert [c["id"] for c in details["auto"]["candidates"]] == ["acme-website-2026", "riverbend-brand-assets"]


@pytest.mark.parametrize("value", CORRUPT_STORED)
def test_a_corrupt_stored_binding_still_reports_incomplete_discovery(tmp_path, monkeypatch, value):
    root = copy_library(tmp_path)
    home = home_tree(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    cwd = str(home / "clients" / "shared")
    # A competing project whose locations cannot be trusted leaves the folder match unprovable.
    # Unlike a permission bit, an unparsable value makes the record unreadable the same way on
    # every host, so this case is not skipped anywhere.
    sealed = root / "Riverbend Dental" / "Projects" / "Brand Assets.md"
    sealed.write_text(sealed.read_text().replace("locations: [~/clients/shared]",
                                                 "locations: ~/clients/shared"))
    details = _corrupt_details(root, cwd, value)
    control = _auto_with_nothing_stored(root, cwd)
    assert details["auto"] == control
    assert (details["auto"]["state"], details["auto"]["reason"]) == ("unbound", "discovery_incomplete")
    assert details["auto"]["unreadable"] == ["Riverbend Dental/Projects/Brand Assets.md"]


def test_an_explicit_bind_repairs_a_stored_binding_that_cannot_be_read(tmp_path):
    root = copy_library(tmp_path)
    session, state = make_session(root)
    store(state, root, "sess-1", ["bad"])
    result = call("engagements_bind", {"client": "acme-bakery"}, session)
    assert result["ok"] is True, result
    assert stored_binding(state, root, "sess-1")["client"] == "acme-bakery"
    assert call("engagements_context", {}, session)["ok"] is True


def test_bind_with_a_missing_library_is_library_missing(tmp_path):
    session, state = make_session(tmp_path / "absent")
    result = call("engagements_bind", {"client": "acme-bakery"}, session)
    assert result["ok"] is False
    assert result["error"]["code"] == "library_missing"
    assert state == {}


# --- context when nothing resolves ------------------------------------------


def test_context_with_nothing_stored_is_auto_unbound(tmp_path):
    root = copy_library(tmp_path)
    session, state = make_session(root)
    details = unbound_details(call("engagements_context", {}, session))
    assert details["mode"] == "auto"
    assert details["state"] == "unbound"
    assert details["reason"] == details["auto"]["reason"]
    assert details["auto"]["state"] == "unbound"
    assert details["candidates"] == []
    assert state == {}


def test_context_after_bind_off_reports_mode_off(tmp_path):
    root = copy_library(tmp_path)
    session, state = make_session(root)
    bound = call("engagements_bind", {"mode": "off"}, session)
    assert bound["ok"], bound
    assert bound["data"]["mode"] == "off"
    assert bound["data"]["state"] == "unbound"
    assert bound["data"]["client"] is None and bound["data"]["project"] is None
    assert bound["data"]["active_projects"] == []
    details = unbound_details(call("engagements_context", {}, session))
    assert details["mode"] == "off"
    assert details["state"] == "unbound"
    assert details["evidence"] == "off"


def test_context_with_a_client_only_binding_asked_for_a_project(tmp_path):
    root = copy_library(tmp_path)
    session, state = make_session(root)
    store(state, root, "sess-1", {"mode": "explicit", "client": "acme-bakery", "project": None,
                                  "at": _bound_at()})
    client_read = call("engagements_context", {}, session)
    assert client_read["ok"], client_read
    assert client_read["data"]["id"] == "acme-bakery"
    details = unbound_details(call("engagements_context", {"kind": "project"}, session))
    assert details["mode"] == "explicit"
    assert details["state"] == "bound"
    assert details["reason"] == "no_project"


def test_a_stored_binding_to_an_inactive_record_reads_unbound(tmp_path):
    root = copy_library(tmp_path)
    session, state = make_session(root)
    store(state, root, "sess-1", {"mode": "explicit", "client": "maple-street-florist",
                                  "project": None, "at": _bound_at()})
    details = unbound_details(call("engagements_context", {}, session))
    assert details["mode"] == "explicit"
    assert details["state"] == "invalid_binding"
    assert details["reason"] == "record_inactive"
    assert details["auto"]["state"] == "unbound"


def test_an_ambiguous_cwd_is_unbound_with_candidates(tmp_path, monkeypatch):
    root = copy_library(tmp_path)
    home = home_tree(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    session, _ = make_session(root, cwd=str(home / "clients" / "shared"))
    details = unbound_details(call("engagements_context", {}, session))
    assert details["mode"] == "auto"
    assert details["state"] == "ambiguous"
    assert [candidate["id"] for candidate in details["candidates"]] == [
        "acme-website-2026", "riverbend-brand-assets"]
    assert details["auto"]["state"] == "ambiguous"


def test_an_unsupported_surface_is_cwd_unsupported(tmp_path):
    root = copy_library(tmp_path)
    home = home_tree(tmp_path)
    session, _ = make_session(root, cwd=str(home / "clients" / "acme" / "website-2026"), surface="telegram")
    details = unbound_details(call("engagements_context", {}, session))
    assert details["state"] == "cwd_unsupported"
    assert details["auto"]["state"] == "cwd_unsupported"


def test_desktop_resolves_the_project_folder_like_the_cli(tmp_path, monkeypatch):
    root = copy_library(tmp_path)
    home = home_tree(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    session, _ = make_session(root, cwd=str(home / "clients" / "acme" / "website-2026"), surface="desktop")
    result = call("engagements_context", {}, session)
    assert result["ok"] is True
    cli, _ = make_session(root, cwd=session.cwd, surface="cli")
    assert call("engagements_context", {}, cli)["data"]["id"] == result["data"]["id"]


def test_context_without_a_session_id_is_no_session(tmp_path):
    root = copy_library(tmp_path)
    session, state = make_session(root, session_id=None)
    details = unbound_details(call("engagements_context", {}, session))
    assert details["mode"] == "auto"
    assert details["state"] == "unbound"
    assert details["reason"] == "no_session"
    assert details["evidence"] == "no session"
    assert details["auto"] == {"state": "unbound", "reason": "no_session", "evidence": "no session",
                               "candidates": []}
    assert state == {}


# --- explicit bindings and their data ---------------------------------------


def test_bind_explicit_then_context_round_trip(tmp_path):
    root = copy_library(tmp_path)
    session, state = make_session(root)
    bound = call("engagements_bind", {"client": "acme-bakery"}, session)
    assert bound["ok"], bound
    data = bound["data"]
    assert data["mode"] == "explicit"
    assert data["state"] == "bound"
    assert data["method"] == "explicit"
    assert data["evidence"] == "explicit 14:02"
    assert data["client"]["id"] == "acme-bakery"
    assert [section["title"] for section in data["client"]["sections"]] == [
        "Summary", "Contacts", "Locations"]
    assert [project["id"] for project in data["active_projects"]] == ["acme-website-2026"]
    assert data["project"] is None

    read = call("engagements_context", {}, session)
    assert read["ok"], read
    assert read["data"]["id"] == "acme-bakery"
    assert read["data"]["kind"] == "client"
    assert read["data"]["properties"]["status"] == "active"
    assert [project["id"] for project in read["data"]["projects"]] == ["acme-website-2026"]
    assert stored_binding(state, root, "sess-1")["client"] == "acme-bakery"


def test_bind_project_then_context_kind_project(tmp_path):
    root = copy_library(tmp_path)
    session, _ = make_session(root)
    bound = call("engagements_bind", {"project": "acme-website-2026"}, session)
    assert bound["ok"], bound
    assert bound["data"]["client"]["id"] == "acme-bakery"
    assert bound["data"]["project"]["id"] == "acme-website-2026"
    assert bound["data"]["project"]["locations"] == [
        "~/clients/acme/website-2026", "~/clients/shared", "https://git.example/studio/acme-site"]

    read = call("engagements_context", {"kind": "project"}, session)
    assert read["ok"], read
    assert read["data"]["id"] == "acme-website-2026"
    assert read["data"]["kind"] == "project"
    assert read["data"]["client"] == {"id": "acme-bakery", "name": "Acme Bakery"}


def test_bind_mode_rules(tmp_path):
    root = copy_library(tmp_path)
    session, state = make_session(root)
    explicit = call("engagements_bind", {"mode": "explicit"}, session)
    assert explicit["error"]["code"] == "invalid_op"
    assert explicit["error"]["details"] == {"field": "mode", "reason": "explicit_requires_target"}
    for args, field in (({"mode": "off", "client": "acme-bakery"}, "client"),
                        ({"mode": "auto", "project": "acme-website-2026"}, "project")):
        result = call("engagements_bind", args, session)
        assert result["error"]["code"] == "invalid_op"
        assert result["error"]["details"] == {"field": field, "reason": "mode_forbids_target"}
    assert state == {}


def test_bind_refuses_a_project_from_another_client(tmp_path):
    root = copy_library(tmp_path)
    session, state = make_session(root)
    result = call("engagements_bind", {"client": "riverbend-dental", "project": "acme-website-2026"},
                  session)
    assert result["error"]["code"] == "invalid_binding"
    assert result["error"]["message"] == (
        "Project acme-website-2026 belongs to acme-bakery, not riverbend-dental. Nothing was bound.")
    assert result["error"]["details"] == {"project": "acme-website-2026", "project_client": "acme-bakery",
                                          "requested_client": "riverbend-dental"}
    assert state == {}


def test_the_binding_key_includes_the_library_id(tmp_path):
    root = copy_library(tmp_path)
    session, state = make_session(root)
    assert call("engagements_bind", {"client": "acme-bakery"}, session)["ok"] is True
    key = binding_key_for(root, "sess-1")
    assert key == f"binding:{library_id(root)}:sess-1"
    assert [stored for stored in state if stored.startswith("binding:")] == [key]
    assert stored_binding(state, root, "sess-1")["mode"] == "explicit"
    assert binding_key_for(root, "sess-2") != key


def test_bind_auto_after_explicit_resets_to_auto(tmp_path):
    root = copy_library(tmp_path)
    session, state = make_session(root)
    assert call("engagements_bind", {"client": "acme-bakery"}, session)["ok"] is True
    reset = call("engagements_bind", {"mode": "auto"}, session)
    assert reset["ok"], reset
    assert reset["data"]["mode"] == "auto"
    assert reset["data"]["state"] == "unbound"
    assert reset["data"]["client"] is None
    stored = stored_binding(state, root, "sess-1")
    assert stored["mode"] == "auto"
    assert stored.get("client") is None
    unbound_details(call("engagements_context", {}, session))


def test_auto_bind_disabled_reports_unbound(tmp_path):
    # TOOLS.md "engagements_bind" (Data) has no reason field, so the reason is
    # asserted through the evidence binding produces and through the next context call.
    root = copy_library(tmp_path)
    session, _ = make_session(root, auto_bind=False)
    bound = call("engagements_bind", {"mode": "auto"}, session)
    assert bound["ok"], bound
    assert bound["data"]["mode"] == "auto"
    assert bound["data"]["state"] == "unbound"
    assert "auto_bind" in bound["data"]["evidence"]
    details = unbound_details(call("engagements_context", {}, session))
    assert details["mode"] == "auto"
    assert details["state"] == "unbound"
    assert details["reason"] == "auto_bind_disabled"


def test_cwd_inside_a_project_location_binds_automatically(tmp_path, monkeypatch):
    root = copy_library(tmp_path)
    home = home_tree(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    session, state = make_session(root, cwd=str(home / "clients" / "acme" / "website-2026"))
    client_read = call("engagements_context", {}, session)
    assert client_read["ok"], client_read
    assert client_read["data"]["id"] == "acme-bakery"
    project_read = call("engagements_context", {"kind": "project"}, session)
    assert project_read["ok"], project_read
    assert project_read["data"]["id"] == "acme-website-2026"
    assert project_read["data"]["client"]["id"] == "acme-bakery"
    assert state == {}


# --- isolation and state hygiene --------------------------------------------


def test_two_sessions_sharing_one_state_do_not_cross(tmp_path):
    root = copy_library(tmp_path)
    _, state = make_session(root, session_id="sess-unused")
    first, _ = make_session(root, session_id="sess-a", state=state)
    second, _ = make_session(root, session_id="sess-b", state=state)
    barrier = threading.Barrier(2, timeout=30)

    def work(session, client_id):
        barrier.wait()
        bound = call("engagements_bind", {"client": client_id}, session)
        assert bound["ok"], bound
        for _ in range(5):
            read = call("engagements_context", {}, session)
            assert read["ok"], read
            assert read["data"]["id"] == client_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(work, first, "acme-bakery"), pool.submit(work, second, "riverbend-dental")]
        for future in futures:
            future.result()
    assert stored_binding(state, root, "sess-a")["client"] == "acme-bakery"
    assert stored_binding(state, root, "sess-b")["client"] == "riverbend-dental"


def test_reads_and_writes_never_touch_the_binding_state(tmp_path):
    root = copy_library(tmp_path)
    session, state = make_session(root)
    assert call("engagements_bind", {"client": "acme-bakery"}, session)["ok"] is True
    before = copy.deepcopy(state)
    base = call("engagements_context", {"id": "acme-bakery"}, session)["data"]["rev"]
    for name, args in (
        ("engagements_find", {"query": "acme"}),
        ("engagements_context", {"id": "acme-bakery"}),
        ("engagements_context", {}),
        ("engagements_create", {"kind": "client", "name": "Untouched State Co"}),
        ("engagements_update", {"id": "acme-bakery", "base_rev": base, "source": "test",
                                "ops": [{"op": "set_property", "key": "status", "value": "paused"}]}),
    ):
        result = call(name, args, session)
        assert result["ok"] is True, (name, result)
        assert state == before, name


def test_state_io_failures_are_io_errors(tmp_path):
    root = copy_library(tmp_path)

    def boom(*_args):
        raise OSError("boom /etc/shadow")

    reader = session_with_state(root, boom, lambda key, value: None)
    read = call("engagements_context", {}, reader)
    assert read["ok"] is False
    assert read["error"]["code"] == "io_error"
    assert isinstance(read["error"]["details"], dict)
    text = json.dumps(read)
    assert "Traceback" not in text
    assert "boom" not in text
    assert "/etc/shadow" not in text

    writer = session_with_state(root, lambda key: None, boom)
    bound = call("engagements_bind", {"client": "acme-bakery"}, writer)
    assert bound["ok"] is False
    assert bound["error"]["code"] == "io_error"
    assert "boom" not in json.dumps(bound)


@pytest.mark.parametrize("client_id", ["acme-bakery", "riverbend-dental"])
def test_bind_and_context_read_the_same_client(tmp_path, client_id):
    root = copy_library(tmp_path)
    session, _ = make_session(root)
    assert call("engagements_bind", {"client": client_id}, session)["ok"] is True
    read = call("engagements_context", {}, session)
    assert read["data"]["id"] == client_id
    assert read["data"]["kind"] == "client"


# --- configured labels in the messages the tools return ------------------------------------

def session_with_labels(root, labels, state=None):
    from engagements.tools import Session, SessionConfig

    store = {} if state is None else state

    def state_get(key):
        return store.get(key)

    def state_set(key, value):
        store[key] = value

    return Session(session_id="sess-1", cwd=None, surface="cli", profile="studio-dev",
                   config=SessionConfig(library_root=root, labels=labels),
                   state_get=state_get, state_set=state_set, clock=fixed_clock), store


def test_a_configured_label_reaches_the_unbound_error_message(tmp_path):
    root = copy_library(tmp_path)
    session, _ = session_with_labels(root, {"client": "Account", "project": "Matter"})
    error = call("engagements_context", {}, session)["error"]
    assert error["code"] == "unbound"
    assert error["message"] == "No id or path was given and no Account or Matter is bound to this session."


def test_a_configured_label_reaches_the_stored_unreadable_message(tmp_path):
    root = copy_library(tmp_path)
    session, state = session_with_labels(root, {"client": "Account", "project": "Matter"})
    store(state, root, "sess-1", ["bad"])
    error = call("engagements_context", {}, session)["error"]
    assert error["message"] == ("The stored binding is unreadable. Nothing is bound; do not rely on Account context "
                                "from earlier turns. Pick Account with engagements_bind.")


def test_a_configured_label_reaches_the_not_found_message(tmp_path):
    root = copy_library(tmp_path)
    session, _ = session_with_labels(root, {"client": "Account", "project": "Matter"})
    error = call("engagements_update", {"id": "no-such-record", "base_rev": "0123456789abcdef",
                                        "source": "synthetic probe",
                                        "ops": [{"op": "set_property", "key": "status", "value": "active"}]},
                 session)["error"]
    assert error["code"] == "not_found"
    assert error["message"] == "No Account or Matter with id 'no-such-record'."
