"""Automatic binding never picks a client from a library it could not read in full.

The rule under test: a folder binds only when every record that could have matched it
was actually read. A record the walk could not read, a record that no longer holds what
discovery found, an entry whose kind or id could not be determined, a project whose
status could not be read, and a project whose ``locations`` the parser did not produce
cleanly all leave the folder match unprovable, so a single match is reported as
``unbound`` with reason ``discovery_incomplete`` instead of binding a client. Several
matches stay ``ambiguous`` and no match stays ``no_match``, since neither picks a
client. Explicit binding is untouched.
"""

import datetime as dt
import json
import os
import sys

import pytest
import yaml
from engagements import runtime, tools
from engagements.binding import (
    BindingState,
    match_projects,
    resolve_binding,
    stored_after,
)
from engagements.library import open_library
from tools_support import call, make_session, stored_binding

IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0
NEEDS_MODES = pytest.mark.skipif(IS_ROOT or sys.platform == "win32",
                                 reason="permission bits do not bind as root or on Windows")

HOME = "/home/ann"
NOW = dt.datetime(2026, 11, 9, 12, tzinfo=dt.UTC)


# --- pure matching and resolution ---------------------------------------------------


def everywhere(_path):
    return True


def project_row(pid, cid, locations, *, name=None, status="active", valid=True):
    return {"id": pid, "kind": "project", "name": name or pid, "client": cid,
            "status": status, "valid": valid, "locations": list(locations)}


def record_row(rid, kind, *, client=None, status="active", valid=True):
    return {"id": rid, "kind": kind, "name": rid, "client": client, "status": status, "valid": valid}


ACME_WEBSITE = project_row("acme-website-2026", "acme-bakery", ["/work/shared", "/work/acme"])
RIVERBEND_BRAND = project_row("riverbend-brand-assets", "riverbend-dental", ["/work/shared"])
WORLD = [record_row("acme-bakery", "client"), record_row("riverbend-dental", "client"),
         ACME_WEBSITE, RIVERBEND_BRAND]


def match(cwd, projects, *, incomplete=()):
    return match_projects(cwd, projects, surface="cli", home=HOME, os_name="posix", exists=everywhere,
                          realpath=lambda path: path, incomplete=incomplete)


def resolve(stored, cwd="/work/shared", *, projects=(), records=WORLD, incomplete=()):
    return resolve_binding(stored, list(projects), records, cwd, "cli", home=HOME, os_name="posix",
                           exists=everywhere, realpath=lambda path: path, incomplete=incomplete)


def test_a_complete_library_binds_the_one_match():
    found = match("/work/shared/site", [ACME_WEBSITE])
    assert (found.state, found.client) == ("bound", "acme-bakery")


def test_one_match_on_an_incomplete_library_is_not_a_binding():
    found = match("/work/shared/site", [ACME_WEBSITE], incomplete=("Riverbend Dental/Brand Assets.md",))
    assert (found.state, found.reason) == ("unbound", "discovery_incomplete")
    assert found.client is None and found.project is None
    assert found.unreadable == ("Riverbend Dental/Brand Assets.md",)


def test_several_matches_on_an_incomplete_library_stay_ambiguous():
    found = match("/work/shared", [ACME_WEBSITE, RIVERBEND_BRAND], incomplete=("other.md",))
    assert found.state == "ambiguous" and found.unreadable == ()
    assert [candidate["id"] for candidate in found.candidates] == [
        "acme-website-2026", "riverbend-brand-assets"]


def test_no_match_on_an_incomplete_library_stays_no_match():
    found = match("/work/elsewhere", [ACME_WEBSITE], incomplete=("other.md",))
    assert (found.state, found.reason) == ("unbound", "no_match")


def test_an_unsupported_surface_does_not_consult_an_incomplete_library():
    found = match_projects("/work/shared", [ACME_WEBSITE], surface="telegram", home=HOME, os_name="posix",
                           exists=everywhere, realpath=lambda path: path, incomplete=("other.md",))
    assert found.state == "cwd_unsupported" and found.unreadable == ()


def test_incomplete_discovery_releases_a_prior_automatic_binding():
    stored = {"mode": "auto", "client": "acme-bakery", "project": "acme-website-2026"}
    state = resolve(stored, projects=[ACME_WEBSITE], incomplete=("other.md",))
    assert (state.state, state.reason) == ("unbound", "discovery_incomplete")
    assert state.client_id is None and state.project_id is None
    assert state.unreadable == ("other.md",)
    assert stored_after(stored, state) == {"mode": "auto"}


def test_incomplete_discovery_does_not_touch_an_explicit_binding():
    stored = {"mode": "explicit", "client": "acme-bakery", "project": "acme-website-2026",
              "at": "2026-11-09T12:00:00+00:00"}
    state = resolve(stored, incomplete=("other.md",))
    assert (state.state, state.method) == ("bound", "explicit")
    assert state.client_id == "acme-bakery"


def test_the_reason_counts_the_records_it_could_not_read():
    state = resolve(None, projects=[ACME_WEBSITE], incomplete=("a.md", "b.md"))
    assert state.reason == "discovery_incomplete"
    assert state.unreadable == ("a.md", "b.md")
    assert "2 records could not be read (a.md, b.md)" in state.evidence


# --- a real library on disk ------------------------------------------------------------


def use_home(monkeypatch, home):
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))


def record_text(kind, rid, name, *, client=None, locations=None, status="active", extra=()):
    """One record's bytes. ``extra`` lines go inside the frontmatter, for malformed shapes."""
    properties = {"engagements": kind}
    if rid is not None:
        properties["id"] = rid
    properties["name"] = name
    if status is not None:
        properties["status"] = status
    if client is not None:
        properties["client"] = client
    if locations is not None:
        properties["locations"] = list(locations)
    front = yaml.safe_dump(properties, sort_keys=False) + "".join(f"{line}\n" for line in extra)
    return ("---\n" + front + "---\n\n## Summary\n\nFictional.\n").encode("utf-8")


def write(root, relpath, data):
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


BRAND_ASSETS = "Riverbend Dental/Projects/Brand Assets.md"


def shared_library(tmp_path, monkeypatch):
    """Two clients whose active projects list one existing folder, plus one only Acme lists."""
    home = tmp_path / "home"
    shared = home / "clients" / "shared"
    acme_folder = home / "clients" / "acme"
    acme_folder.mkdir(parents=True)
    shared.mkdir()
    use_home(monkeypatch, home)
    root = tmp_path / "library"
    open_library(root, create_default=True)
    root = root.resolve()
    write(root, "Acme Bakery/Client.md", record_text("client", "acme-bakery", "Acme Bakery"))
    write(root, "Riverbend Dental/Client.md", record_text("client", "riverbend-dental", "Riverbend Dental"))
    write(root, "Acme Bakery/Projects/Website.md",
          record_text("project", "acme-website-2026", "Website 2026", client="acme-bakery",
                      locations=[str(acme_folder), str(shared)]))
    write(root, BRAND_ASSETS,
          record_text("project", "riverbend-brand-assets", "Brand Assets", client="riverbend-dental",
                      locations=[str(shared)]))
    return root, home, shared


def host_for(root, cwd, state=None):
    state = {} if state is None else state
    host = runtime.Host(
        get_setting=lambda key, default: {"library_root": str(root)}.get(key, default),
        default_root=lambda: root,
        state_get=state.get,
        state_set=state.__setitem__,
        session_cwd=lambda: str(cwd),
        session_surface=lambda: "cli",
        profile=lambda: "studio-dev",
        clock=lambda: NOW,
    )
    return host, state


def run(host, name, args, session_id="sess-1"):
    return json.loads(runtime.run_tool(host, name, args, session_id))


# --- the reported defect ---------------------------------------------------------------


@NEEDS_MODES
def test_an_unreadable_competing_project_does_not_bind_the_other_client(tmp_path, monkeypatch):
    """The defect: Acme was bound from a folder Riverbend's unreadable project also lists."""
    root, _home, shared = shared_library(tmp_path, monkeypatch)
    both = call("engagements_bind", {"mode": "auto"}, make_session(root, cwd=str(shared))[0])
    assert both["data"]["state"] == "ambiguous", "the library must start out ambiguous"

    sealed = root / BRAND_ASSETS
    sealed.chmod(0)
    try:
        session, state = make_session(root, cwd=str(shared))
        result = call("engagements_bind", {"mode": "auto"}, session)
    finally:
        sealed.chmod(0o644)
    assert result["ok"] is True, result
    data = result["data"]
    assert data["state"] == "unbound"
    assert data["client"] is None and data["project"] is None and data["candidates"] == []
    assert data["unreadable"] == [BRAND_ASSETS]
    assert "could not be read" in data["evidence"]
    assert "acme-bakery" not in json.dumps(data)
    assert stored_binding(state, root, "sess-1") == {"mode": "auto"}


@NEEDS_MODES
def test_the_read_paths_refuse_an_unreadable_competing_project_too(tmp_path, monkeypatch):
    """A read with no id is a different entry point into the same resolution."""
    root, _home, shared = shared_library(tmp_path, monkeypatch)
    sealed = root / BRAND_ASSETS
    sealed.chmod(0)
    try:
        result = call("engagements_context", {}, make_session(root, cwd=str(shared))[0])
    finally:
        sealed.chmod(0o644)
    assert result["ok"] is False
    assert result["error"]["code"] == "unbound"
    details = result["error"]["details"]
    assert (details["state"], details["reason"]) == ("unbound", "discovery_incomplete")
    assert details["unreadable"] == [BRAND_ASSETS]
    assert details["auto"]["unreadable"] == [BRAND_ASSETS]


# --- discovery it could not complete ---------------------------------------------------


def test_a_project_file_that_no_longer_holds_its_record_does_not_bind(tmp_path, monkeypatch):
    """The bytes that decide the binding are read again; a swap in between is refused."""
    root, _home, shared = shared_library(tmp_path, monkeypatch)
    # Only the Riverbend project lists the folder, so the folder resolves to a pair.
    write(root, "Acme Bakery/Projects/Website.md",
          record_text("project", "acme-website-2026", "Website 2026", client="acme-bakery",
                      locations=[str(_home / "clients" / "acme")]))
    swapped = record_text("project", "someone-else", "Other", client="riverbend-dental",
                          locations=[str(shared)])
    real = tools.writer.read_record

    def read_record(lib, relpath, *, clock, warnings):
        data, revision = real(lib, relpath, clock=clock, warnings=warnings)
        return (swapped, revision) if relpath == BRAND_ASSETS else (data, revision)

    monkeypatch.setattr(tools.writer, "read_record", read_record)
    result = call("engagements_bind", {"mode": "auto"}, make_session(root, cwd=str(shared))[0])
    assert result["ok"] is False, result
    assert result["error"]["code"] == "invalid_binding"
    assert result["error"]["details"]["reason"] == "record_identity_changed"


def test_a_project_without_a_readable_id_does_not_bind(tmp_path, monkeypatch):
    root, _home, shared = shared_library(tmp_path, monkeypatch)
    write(root, BRAND_ASSETS, record_text("project", None, "Brand Assets", client="riverbend-dental",
                                          locations=[str(shared)]))
    result = call("engagements_bind", {"mode": "auto"}, make_session(root, cwd=str(shared))[0])
    assert result["ok"] is True, result
    assert result["data"]["state"] == "unbound"
    assert result["data"]["unreadable"] == [BRAND_ASSETS]


def test_a_record_whose_kind_could_not_be_determined_does_not_bind(tmp_path, monkeypatch):
    root, _home, shared = shared_library(tmp_path, monkeypatch)
    write(root, BRAND_ASSETS, record_text("client", "riverbend-brand-assets", "Brand Assets",
                                          locations=[str(shared)], extra=["engagements: project"]))
    result = call("engagements_bind", {"mode": "auto"}, make_session(root, cwd=str(shared))[0])
    assert result["ok"] is True, result
    assert result["data"]["state"] == "unbound"
    assert result["data"]["unreadable"] == [BRAND_ASSETS]


def test_a_project_whose_status_could_not_be_read_does_not_bind(tmp_path, monkeypatch):
    root, _home, shared = shared_library(tmp_path, monkeypatch)
    write(root, BRAND_ASSETS, record_text("project", "riverbend-brand-assets", "Brand Assets",
                                          client="riverbend-dental", locations=[str(shared)],
                                          extra=["status: active"]))
    result = call("engagements_bind", {"mode": "auto"}, make_session(root, cwd=str(shared))[0])
    assert result["ok"] is True, result
    assert result["data"]["state"] == "unbound"
    assert result["data"]["unreadable"] == [BRAND_ASSETS]


# --- every automatic path agrees --------------------------------------------------------


def test_bind_auto_context_and_the_banner_report_one_reason(tmp_path, monkeypatch):
    root, _home, shared = shared_library(tmp_path, monkeypatch)
    write(root, BRAND_ASSETS, record_text("project", None, "Brand Assets", client="riverbend-dental",
                                          locations=[str(shared)]))
    host, _state = host_for(root, shared)
    bound = run(host, "engagements_bind", {"mode": "auto"}, "bind-path")
    read = run(host, "engagements_context", {}, "context-path")
    banner = runtime.banner_hook(host, session_id="banner-path")

    assert bound["ok"] is True and bound["data"]["state"] == "unbound"
    details = read["error"]["details"]
    assert (details["state"], details["reason"]) == ("unbound", "discovery_incomplete")
    assert bound["data"]["evidence"] == details["evidence"]
    assert bound["data"]["unreadable"] == details["unreadable"] == details["auto"]["unreadable"] == [
        BRAND_ASSETS]
    assert "could not be read" in banner["context"]
    assert BRAND_ASSETS in banner["context"]


@NEEDS_MODES
def test_a_bound_session_moves_to_unbound_when_a_record_became_unreadable(tmp_path, monkeypatch):
    root, home, _shared = shared_library(tmp_path, monkeypatch)
    host, state = host_for(root, home / "clients" / "acme")
    first = run(host, "engagements_bind", {"mode": "auto"})
    assert first["data"]["state"] == "bound" and first["data"]["client"]["id"] == "acme-bakery"
    assert stored_binding(state, root, "sess-1")["client"] == "acme-bakery"
    assert "Acme Bakery" in runtime.banner_hook(host, session_id="sess-1")["context"]

    sealed = root / BRAND_ASSETS
    sealed.chmod(0)
    try:
        retired = runtime.banner_hook(host, session_id="sess-1")
        read = run(host, "engagements_context", {})
    finally:
        sealed.chmod(0o644)
    assert "No client is bound now" in retired["context"]
    assert "could not be read" in retired["context"]
    assert stored_binding(state, root, "sess-1") == {"mode": "auto"}, "the old client was kept"
    details = read["error"]["details"]
    assert (details["state"], details["reason"]) == ("unbound", "discovery_incomplete")


def test_the_client_status_names_the_records_it_could_not_read():
    state = BindingState(mode="auto", state="unbound", method="auto", reason="discovery_incomplete",
                         unreadable=(BRAND_ASSETS,), cwd_display="~/clients/shared",
                         evidence="auto: cwd ~/clients/shared matched one project, but 1 record could not be read")
    text = runtime.render_state(state, runtime.DEFAULT_LABELS)
    assert "Not bound" in text
    assert BRAND_ASSETS in text
    assert "could not be read" in text and "hermes engagements doctor" in text


# --- locations the parser could not produce cleanly ------------------------------------
#
# The parser reports property-local problems without raising: a duplicated key leaves the
# property out of ``properties`` and an unconstructable YAML value leaves a placeholder,
# so neither reaches the matcher as a value it can read. Treating either as "no folders"
# would let a folder with two owners bind the other one, so an untrustworthy ``locations``
# property has to count as unknown and make discovery incomplete.


def locations_block(value):
    """A ``locations`` property as a record file would carry it."""
    return yaml.safe_dump({"locations": value}, sort_keys=False)


def brand_assets_text(blocks):
    """The competing project with ``blocks`` as the tail of its frontmatter."""
    front = ("engagements: project\nid: riverbend-brand-assets\nname: Brand Assets\n"
             "client: riverbend-dental\nstatus: active\n")
    return ("---\n" + front + "".join(blocks) + "---\n\n## Summary\n\nFictional.\n").encode("utf-8")


FAULTS = {
    # Two keys with the same folder: the parser cannot say which one the author meant.
    "duplicate-identical": lambda shared, other: [locations_block([str(shared)]), locations_block([str(shared)])],
    "duplicate-different": lambda shared, other: [locations_block([str(shared)]), locations_block([str(other)])],
    "unsupported-tag": lambda shared, other: [
        "locations: !!probe-unknown-tag " + json.dumps([str(shared)]) + "\n"],
    "scalar-string": lambda shared, other: [locations_block(str(shared))],
    "non-string-item": lambda shared, other: [locations_block([str(shared), 7])],
}


def automatic_results(root, cwd):
    """What each automatic entry point says about this folder, from its own fresh session."""
    host, state = host_for(root, cwd)
    return {
        "bind": run(host, "engagements_bind", {"mode": "auto"}, "bind-path"),
        "context": run(host, "engagements_context", {}, "context-path"),
        "banner": runtime.banner_hook(host, session_id="banner-path"),
        "state": state,
    }


def assert_nothing_bound(results, root, relpath=BRAND_ASSETS):
    """Bind, no-id read and banner all withhold the client and name the record."""
    bound = results["bind"]
    assert bound["ok"] is True, bound
    assert bound["data"]["state"] == "unbound"
    assert bound["data"]["client"] is None and bound["data"]["project"] is None
    assert bound["data"]["unreadable"] == [relpath]
    assert stored_binding(results["state"], root, "bind-path") == {"mode": "auto"}
    assert "acme-bakery" not in json.dumps(bound["data"]), "a client leaked into the result"

    read = results["context"]
    assert read["ok"] is False, read
    details = read["error"]["details"]
    assert (details["state"], details["reason"]) == ("unbound", "discovery_incomplete")
    assert details["unreadable"] == [relpath] and details["auto"]["unreadable"] == [relpath]

    # The banner is clipped to stay under its limit, so it names the record by its
    # leading path components rather than in full.
    banner = results["banner"]["context"]
    assert "Riverbend Dental/Projects/" in banner and "could not be read" in banner
    assert "acme-bakery" not in banner


def assert_bound_to_acme(results, root):
    bound = results["bind"]
    assert bound["ok"] is True and bound["data"]["state"] == "bound", bound
    assert bound["data"]["client"]["id"] == "acme-bakery"
    assert stored_binding(results["state"], root, "bind-path")["client"] == "acme-bakery"
    read = results["context"]
    assert read["ok"] is True and read["data"]["id"] == "acme-bakery", read
    assert "Acme Bakery" in results["banner"]["context"]


@pytest.mark.parametrize("fault", list(FAULTS))
def test_locations_the_parser_could_not_produce_cleanly_never_bind(fault, tmp_path, monkeypatch):
    root, home, shared = shared_library(tmp_path, monkeypatch)
    write(root, BRAND_ASSETS, brand_assets_text(FAULTS[fault](shared, home / "clients" / "riverbend")))
    assert_nothing_bound(automatic_results(root, shared), root)


@pytest.mark.parametrize("fault", list(FAULTS))
def test_locations_that_turn_defective_after_an_earlier_scan_never_bind(fault, tmp_path, monkeypatch):
    """A project read cleanly before is read again once its bytes change, and refuses."""
    from engagements import records

    root, home, shared = shared_library(tmp_path, monkeypatch)
    on_disk = records.parse((root / BRAND_ASSETS).read_bytes())
    assert "locations" not in on_disk.property_defects, "the bytes the first scan sees must start clean"
    assert str(shared) in on_disk.properties["locations"]
    first = automatic_results(root, shared)["bind"]
    assert first["data"]["state"] == "ambiguous", "both projects list the folder before the change"
    write(root, BRAND_ASSETS, brand_assets_text(FAULTS[fault](shared, home / "clients" / "riverbend")))
    assert_nothing_bound(automatic_results(root, shared), root)


@pytest.mark.parametrize("fault", list(FAULTS))
def test_an_existing_automatic_binding_is_released_when_locations_turned_defective(fault, tmp_path, monkeypatch):
    """A later turn must not keep a client whose competitors' folders became unknown."""
    root, home, shared = shared_library(tmp_path, monkeypatch)
    write(root, BRAND_ASSETS, record_text("project", "riverbend-brand-assets", "Brand Assets",
                                          client="riverbend-dental"))
    host, state = host_for(root, home / "clients" / "acme")
    first = runtime.banner_hook(host, session_id="sess-1")
    assert "Acme Bakery" in first["context"]
    assert stored_binding(state, root, "sess-1")["client"] == "acme-bakery"

    write(root, BRAND_ASSETS, brand_assets_text(FAULTS[fault](shared, home / "clients" / "riverbend")))
    retired = runtime.banner_hook(host, session_id="sess-1")
    read = run(host, "engagements_context", {})
    assert "No client is bound now" in retired["context"], retired
    assert "Riverbend Dental/Projects/" in retired["context"]
    assert stored_binding(state, root, "sess-1") == {"mode": "auto"}, "the old client was kept"
    details = read["error"]["details"]
    assert (details["state"], details["reason"]) == ("unbound", "discovery_incomplete")


@pytest.mark.parametrize("blocks", [None, [locations_block([])]], ids=["absent-key", "empty-list"])
def test_a_project_that_lists_no_folders_leaves_the_other_project_the_only_match(blocks, tmp_path, monkeypatch):
    """No locations key and an empty list are still what they say: this project matches nowhere."""
    root, _home, shared = shared_library(tmp_path, monkeypatch)
    write(root, BRAND_ASSETS, brand_assets_text(blocks or []))
    assert_bound_to_acme(automatic_results(root, shared), root)


def test_a_record_with_a_defect_elsewhere_and_clean_locations_still_matches(tmp_path, monkeypatch):
    """Locations that parsed cleanly are trusted, whatever else the record got wrong."""
    root, _home, shared = shared_library(tmp_path, monkeypatch)
    write(root, BRAND_ASSETS, brand_assets_text([locations_block([str(shared)])]).replace(
        b"## Summary\n\nFictional.\n", b"## Purpose\n\none\n\n## Purpose\n\ntwo\n"))
    results = automatic_results(root, shared)
    bound = results["bind"]
    assert bound["ok"] is True and bound["data"]["state"] == "ambiguous", bound
    assert [candidate["id"] for candidate in bound["data"]["candidates"]] == [
        "acme-website-2026", "riverbend-brand-assets"]
    assert "unreadable" not in bound["data"]
    assert results["context"]["error"]["details"]["state"] == "ambiguous"
    banner = results["banner"]["context"]
    assert "acme-website-2026" in banner and "riverbend-brand-assets" in banner


# --- the refusal explains itself with the state that refused -----------------------------


@NEEDS_MODES
def test_a_read_that_fails_once_still_reports_the_refusal_it_made(tmp_path, monkeypatch):
    """The refusal names the read that failed; once that read succeeds the folder binds."""
    root, _home, shared = shared_library(tmp_path, monkeypatch)
    # The competitor lists no folder, so once the failing read clears the folder really
    # does match one project: only the refusal itself is left to report.
    sealed = write(root, BRAND_ASSETS, record_text("project", "riverbend-brand-assets", "Brand Assets",
                                                   client="riverbend-dental"))
    sealed.chmod(0)
    try:
        result = call("engagements_context", {}, make_session(root, cwd=str(shared))[0])
    finally:
        sealed.chmod(0o644)
    assert result["ok"] is False, result
    details = result["error"]["details"]
    assert (details["state"], details["reason"]) == ("unbound", "discovery_incomplete")
    assert details["unreadable"] == [BRAND_ASSETS]
    assert (details["auto"]["state"], details["auto"]["unreadable"]) == ("unbound", [BRAND_ASSETS])
    recovered = call("engagements_context", {}, make_session(root, cwd=str(shared))[0])
    assert recovered["ok"] is True and recovered["data"]["id"] == "acme-bakery", recovered


# --- unchanged ------------------------------------------------------------------------


@NEEDS_MODES
def test_explicit_binding_and_reads_still_work_when_a_record_is_unreadable(tmp_path, monkeypatch):
    root, _home, shared = shared_library(tmp_path, monkeypatch)
    sealed = root / BRAND_ASSETS
    sealed.chmod(0)
    try:
        session, state = make_session(root, cwd=str(shared))
        bound = call("engagements_bind", {"client": "riverbend-dental"}, session)
        read = call("engagements_context", {}, session)
        by_id = call("engagements_context", {"id": "acme-website-2026"}, session)
    finally:
        sealed.chmod(0o644)
    assert bound["ok"] is True, bound
    assert bound["data"]["state"] == "bound" and bound["data"]["client"]["id"] == "riverbend-dental"
    assert stored_binding(state, root, "sess-1")["client"] == "riverbend-dental"
    assert read["ok"] is True and read["data"]["id"] == "riverbend-dental"
    assert by_id["ok"] is True and by_id["data"]["id"] == "acme-website-2026"


@NEEDS_MODES
def test_find_keeps_its_partial_inventory_when_a_record_is_unreadable(tmp_path, monkeypatch):
    root, _home, _shared = shared_library(tmp_path, monkeypatch)
    sealed = root / BRAND_ASSETS
    sealed.chmod(0)
    try:
        found = call("engagements_find", {"query": ""}, make_session(root)[0])
    finally:
        sealed.chmod(0o644)
    assert found["ok"] is True, found
    ids = [row["id"] for row in found["data"]["results"]]
    assert "acme-bakery" in ids and "acme-website-2026" in ids
    assert "riverbend-brand-assets" not in ids


def test_a_malformed_project_that_still_lists_the_folder_still_counts_as_a_match(tmp_path, monkeypatch):
    root, _home, shared = shared_library(tmp_path, monkeypatch)
    # A duplicated section is a defect, but the record still names its folders, so the
    # folder match is still shared: the answer is ambiguous, never a quiet binding.
    write(root, BRAND_ASSETS, record_text("project", "riverbend-brand-assets", "Brand Assets",
                                          client="riverbend-dental", locations=[str(shared)]).replace(
        b"## Summary\n\nFictional.\n", b"## Purpose\n\none\n\n## Purpose\n\ntwo\n"))
    session, _state = make_session(root, cwd=str(shared))
    result = call("engagements_bind", {"mode": "auto"}, session)
    assert result["ok"] is True, result
    assert result["data"]["state"] == "ambiguous"
    assert [candidate["id"] for candidate in result["data"]["candidates"]] == [
        "acme-website-2026", "riverbend-brand-assets"]
    assert "unreadable" not in result["data"]
