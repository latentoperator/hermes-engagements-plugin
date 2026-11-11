"""Spec 6.4 modes, revalidation, section 9 step 4 layout and session-keyed state."""

import datetime as dt
import os
from pathlib import Path

import pytest
from engagements.binding import (
    binding_key,
    clear_binding,
    explicit_binding,
    load_binding,
    migrate,
    resolve_binding,
    store_binding,
    stored_after,
)
from engagements.errors import EngagementsError
from engagements.records import parse

HOME = "/home/ann"
FIXTURES = Path(__file__).parent / "fixtures" / "library"


def everywhere(_path):
    return True


def client(cid, name=None, *, status="active", valid=True):
    return {
        "id": cid,
        "kind": "client",
        "name": name or cid,
        "status": status,
        "client": None,
        "valid": valid,
    }


def project(pid, cid, locations=(), *, name=None, status="active", valid=True):
    return {
        "id": pid,
        "kind": "project",
        "name": name or pid,
        "status": status,
        "client": cid,
        "locations": list(locations),
        "valid": valid,
    }


def world(*extra):
    records = [
        client("acme-bakery", "Acme Bakery"),
        client("riverbend-dental", "Riverbend Dental"),
        project("acme-website-2026", "acme-bakery", ["/work/acme"], name="Website 2026"),
        project("riverbend-recall", "riverbend-dental", ["/work/riverbend"], name="Recall"),
        *extra,
    ]
    projects = [r for r in records if r["kind"] == "project"]
    return projects, records


def resolve(stored, cwd="/work/elsewhere", surface="cli", records=None, projects=None, **kw):
    if records is None:
        projects, records = world()
    kw.setdefault("home", HOME)
    kw.setdefault("os_name", "posix")
    kw.setdefault("exists", everywhere)
    kw.setdefault("realpath", lambda p: p)
    return resolve_binding(stored, projects, records, cwd, surface, **kw)


NOON = dt.datetime(2026, 11, 7, 14, 2, tzinfo=dt.timezone(dt.timedelta(hours=-6)))


def clock():
    return NOON


# --- auto --------------------------------------------------------------------


def test_null_and_absent_state_read_as_auto():
    got = resolve(None, cwd="/work/acme/site")
    assert got.mode == "auto" and got.method == "auto" and got.state == "bound"
    assert got.client_id == "acme-bakery" and got.client_name == "Acme Bakery"
    assert got.project_id == "acme-website-2026" and got.project_name == "Website 2026"
    assert got.evidence == "auto: cwd /work/acme/site matched project acme-website-2026"


def test_auto_evidence_collapses_home_like_the_client_command():
    projects, records = world(project("home-proj", "acme-bakery", ["~/clients/acme"]))
    got = resolve({"mode": "auto"}, cwd="/home/ann/clients/acme", projects=projects, records=records)
    assert got.state == "bound"
    assert got.evidence == "auto: cwd ~/clients/acme matched project home-proj"


def test_auto_unbound_when_nothing_matches():
    got = resolve({"mode": "auto"}, cwd="/somewhere/else")
    assert (got.mode, got.state, got.method) == ("auto", "unbound", "auto")
    assert got.client_id is None and got.project_id is None


def test_auto_ambiguous_lists_candidates_and_binds_nothing():
    projects, records = world(project("acme-menu", "acme-bakery", ["/work/acme"]))
    got = resolve(None, cwd="/work/acme", projects=projects, records=records)
    assert got.state == "ambiguous"
    assert [c["id"] for c in got.candidates] == ["acme-menu", "acme-website-2026"]
    assert got.client_id is None and got.project_id is None


@pytest.mark.parametrize("surface", ["telegram", "local", "acp"])
def test_auto_on_unsupported_surface_reports_cwd_unsupported(surface):
    got = resolve(None, cwd="/work/acme", surface=surface)
    assert got.state == "cwd_unsupported" and got.client_id is None


def test_auto_on_desktop_resolves_the_project_folder():
    """Desktop Projects set the session cwd (RUNTIME-EVIDENCE Phase 3D), so Desktop binds like the CLI."""
    got = resolve(None, cwd="/work/acme", surface="desktop")
    assert got.state != "cwd_unsupported"


def test_auto_bind_setting_off_suppresses_cwd_resolution():
    got = resolve(None, cwd="/work/acme", auto_bind=False)
    assert got.state == "unbound" and got.reason == "auto_bind_disabled"


def test_auto_binding_to_an_inactive_client_is_invalid_not_substituted():
    projects, records = world()
    records[0] = client("acme-bakery", "Acme Bakery", status="former")
    got = resolve(None, cwd="/work/acme", projects=projects, records=records)
    assert got.state == "invalid_binding"
    assert got.reason == "record_inactive"
    assert got.client_id is None and got.project_id is None


def test_auto_binding_to_a_malformed_project_is_invalid():
    projects, records = world(project("acme-shop", "acme-bakery", ["/work/shop"], valid=False))
    got = resolve(None, cwd="/work/shop", projects=projects, records=records)
    assert got.state == "invalid_binding" and got.reason == "record_malformed"


def test_auto_binding_with_a_missing_client_is_invalid():
    projects, records = world(project("orphan", "ghost-client", ["/work/orphan"]))
    got = resolve(None, cwd="/work/orphan", projects=projects, records=records)
    assert got.state == "invalid_binding" and got.reason == "record_missing"


# --- auto: the selected project is checked against the current records ---------------


def _replace_record(records, rid, replacement):
    index = next(i for i, r in enumerate(records) if r["id"] == rid)
    if replacement is None:
        records.pop(index)
    else:
        records[index] = replacement


@pytest.mark.parametrize(
    ("replacement", "reason"),
    [
        (None, "record_missing"),
        (project("acme-website-2026", "acme-bakery", ["/work/acme"], valid=False), "record_malformed"),
        (project("acme-website-2026", "acme-bakery", ["/work/acme"], status="historical"), "record_inactive"),
        ({**client("acme-website-2026"), "kind": "client"}, "kind_mismatch"),
        (project("acme-website-2026", "riverbend-dental", ["/work/acme"]), "client_changed"),
    ],
)
def test_auto_selected_project_must_be_usable_in_the_current_records(replacement, reason):
    # The projects list still offers a clean, active Acme project; the record index disagrees.
    projects, records = world()
    _replace_record(records, "acme-website-2026", replacement)
    got = resolve(None, cwd="/work/acme", projects=projects, records=records)
    assert got.state == "invalid_binding" and got.reason == reason
    assert got.client_id is None and got.project_id is None
    assert got.attempted_project == "acme-website-2026"


def test_auto_selected_project_with_a_duplicated_record_id_is_invalid():
    projects, records = world()
    records.append(project("acme-website-2026", "acme-bakery", ["/elsewhere"], name="Copy"))
    got = resolve(None, cwd="/work/acme", projects=projects, records=records)
    assert got.state == "invalid_binding" and got.reason == "duplicate_id"
    assert got.client_id is None


# --- auto: the previous automatic binding is revalidated each turn -------------------


def turn(stored, cwd, projects, records):
    """One hook turn: resolve, then persist what the session should remember."""
    state = resolve(stored, cwd=cwd, projects=projects, records=records)
    return state, stored_after(stored, state)


def test_a_fresh_auto_binding_is_remembered_in_session_state():
    projects, records = world()
    state, stored = turn({"mode": "auto"}, "/work/acme", projects, records)
    assert state.state == "bound"
    assert stored == {"mode": "auto", "client": "acme-bakery", "project": "acme-website-2026"}
    assert turn(None, "/work/acme", projects, records)[1] == stored


def test_reparenting_the_auto_bound_project_is_invalid_not_another_client():
    projects, records = world()
    _, stored = turn(None, "/work/acme", projects, records)
    # Same project, same folder, now listed under Riverbend in BOTH inputs.
    projects[0]["client"] = "riverbend-dental"
    _replace_record(records, "acme-website-2026",
                    project("acme-website-2026", "riverbend-dental", ["/work/acme"]))
    for _ in range(2):  # stays invalid on later turns; it is not silently re-resolved
        state, stored = turn(stored, "/work/acme", projects, records)
        assert state.state == "invalid_binding" and state.reason == "client_changed"
        assert state.client_id is None and state.project_id is None
        assert state.attempted_client == "acme-bakery"
        assert state.attempted_project == "acme-website-2026"
        assert stored["client"] == "acme-bakery"


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda p, r: (p.pop(0), _replace_record(r, "acme-website-2026", None)), "record_missing"),
        (lambda p, r: (p[0].update(status="historical"),
                       _replace_record(r, "acme-website-2026",
                                       project("acme-website-2026", "acme-bakery", ["/work/acme"],
                                               status="historical"))), "record_inactive"),
        (lambda p, r: (p[0].update(valid=False),
                       _replace_record(r, "acme-website-2026",
                                       project("acme-website-2026", "acme-bakery", ["/work/acme"],
                                               valid=False))), "record_malformed"),
        (lambda p, r: _replace_record(r, "acme-bakery", client("acme-bakery", status="former")),
         "record_inactive"),
        (lambda p, r: _replace_record(r, "acme-bakery", None), "record_missing"),
    ],
)
def test_auto_bound_record_that_goes_bad_is_invalid_even_with_a_replacement_match(mutate, reason):
    projects, records = world()
    _, stored = turn(None, "/work/acme", projects, records)
    mutate(projects, records)
    # A replacement project of another client now claims the same folder.
    extra = project("riverbend-takeover", "riverbend-dental", ["/work/acme"])
    projects.append(extra)
    records.append(extra)
    state, after = turn(stored, "/work/acme", projects, records)
    assert state.state == "invalid_binding" and state.reason == reason
    assert state.client_id is None and state.project_id is None
    assert after == stored


def test_stored_auto_identity_is_revalidated_without_a_prior_turn():
    projects, records = world()
    _replace_record(records, "acme-website-2026",
                    project("acme-website-2026", "riverbend-dental", ["/work/acme"]))
    projects[0]["client"] = "riverbend-dental"
    stored = {"mode": "auto", "client": "acme-bakery", "project": "acme-website-2026"}
    got = resolve(stored, cwd="/work/acme", projects=projects, records=records)
    assert got.state == "invalid_binding" and got.reason == "client_changed"


def test_a_legitimate_cwd_change_still_resolves_and_is_remembered():
    projects, records = world()
    _, stored = turn(None, "/work/acme", projects, records)
    state, stored = turn(stored, "/work/riverbend", projects, records)
    assert state.state == "bound" and state.client_id == "riverbend-dental"
    assert stored == {"mode": "auto", "client": "riverbend-dental", "project": "riverbend-recall"}
    state, stored = turn(stored, "/nowhere", projects, records)
    assert state.state == "unbound" and stored == {"mode": "auto"}


def test_returning_to_plain_auto_clears_an_invalid_remembered_binding():
    projects, records = world()
    _, stored = turn(None, "/work/acme", projects, records)
    _replace_record(records, "acme-website-2026", None)
    projects.pop(0)
    assert turn(stored, "/work/riverbend", projects, records)[0].state == "invalid_binding"
    # engagements_bind mode "auto" / `/client auto` stores plain auto again.
    state, _ = turn({"mode": "auto"}, "/work/riverbend", projects, records)
    assert state.state == "bound" and state.client_id == "riverbend-dental"


def test_auto_remembers_nothing_when_auto_resolution_does_not_run():
    projects, records = world()
    stored = {"mode": "auto", "client": "acme-bakery", "project": "acme-website-2026"}
    got = resolve(stored, cwd="/work/acme", projects=projects, records=records, auto_bind=False)
    assert got.state == "unbound"
    assert stored_after(stored, got) == {"mode": "auto"}


def test_explicit_and_off_state_is_never_rewritten_by_a_turn():
    projects, records = world()
    for stored in (explicit_binding("acme-bakery", clock=clock), {"mode": "off"}):
        state = resolve(stored, cwd="/work/acme", projects=projects, records=records)
        assert stored_after(stored, state) is stored


def test_a_fresh_invalid_auto_result_is_not_remembered():
    projects, records = world()
    records[0] = client("acme-bakery", status="former")
    state, stored = turn({"mode": "auto"}, "/work/acme", projects, records)
    assert state.state == "invalid_binding"
    assert stored == {"mode": "auto"}
    records[0] = client("acme-bakery", "Acme Bakery")
    assert turn(stored, "/work/acme", projects, records)[0].state == "bound"


@pytest.mark.parametrize(
    "stored",
    [
        {"mode": "auto", "client": "acme-bakery"},
        {"mode": "auto", "project": "acme-website-2026"},
        {"mode": "auto", "client": 7, "project": "acme-website-2026"},
        {"mode": "auto", "client": "acme-bakery", "project": ""},
    ],
)
def test_a_garbled_remembered_auto_identity_is_unreadable(stored):
    got = resolve(stored, cwd="/work/acme")
    assert got.state == "invalid_binding" and got.reason == "stored_unreadable"


def test_store_binding_accepts_a_remembered_auto_identity():
    store = Store()
    value = {"mode": "auto", "client": "acme-bakery", "project": "acme-website-2026"}
    store_binding(store.set, "lib", "s", value)
    assert load_binding(store.get, "lib", "s") == value


# --- off ---------------------------------------------------------------------


def test_off_suppresses_auto_even_inside_a_project_folder():
    got = resolve({"mode": "off"}, cwd="/work/acme/site")
    assert (got.mode, got.state, got.method) == ("off", "unbound", "off")
    assert got.client_id is None
    assert got.evidence == "off"


# --- explicit ----------------------------------------------------------------


def test_explicit_client_binds_and_ignores_cwd():
    stored = explicit_binding("riverbend-dental", clock=clock)
    got = resolve(stored, cwd="/work/acme/site")
    assert (got.mode, got.state, got.method) == ("explicit", "bound", "explicit")
    assert got.client_id == "riverbend-dental" and got.project_id is None
    assert got.evidence == "explicit 14:02"


def test_explicit_project_binds_project_and_client():
    stored = explicit_binding("acme-bakery", "acme-website-2026", clock=clock)
    got = resolve(stored)
    assert got.state == "bound"
    assert (got.client_id, got.project_id) == ("acme-bakery", "acme-website-2026")


def test_explicit_binding_works_on_surfaces_without_cwd():
    stored = explicit_binding("acme-bakery", clock=clock)
    assert resolve(stored, surface="telegram", cwd=None).state == "bound"


def test_explicit_binding_wins_over_a_cwd_that_matches_another_project():
    stored = explicit_binding("riverbend-dental", clock=clock)
    got = resolve(stored, cwd="/work/acme")
    assert got.client_id == "riverbend-dental"


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda recs: recs.__setitem__(0, client("acme-bakery", status="paused")), "record_inactive"),
        (lambda recs: recs.__setitem__(0, client("acme-bakery", status="former")), "record_inactive"),
        (lambda recs: recs.__setitem__(0, client("acme-bakery", valid=False)), "record_malformed"),
        (lambda recs: recs.pop(0), "record_missing"),
    ],
)
def test_explicit_client_revalidation(mutate, reason):
    projects, records = world()
    mutate(records)
    got = resolve(explicit_binding("acme-bakery", clock=clock), projects=projects, records=records)
    assert got.state == "invalid_binding" and got.reason == reason
    assert got.client_id is None and got.project_id is None
    assert got.attempted_client == "acme-bakery"


@pytest.mark.parametrize(
    ("replacement", "reason"),
    [
        (project("acme-website-2026", "acme-bakery", status="historical"), "record_inactive"),
        (project("acme-website-2026", "acme-bakery", valid=False), "record_malformed"),
        (project("acme-website-2026", "riverbend-dental"), "client_changed"),
        (None, "record_missing"),
    ],
)
def test_explicit_project_revalidation(replacement, reason):
    projects, records = world()
    index = next(i for i, r in enumerate(records) if r["id"] == "acme-website-2026")
    if replacement is None:
        records.pop(index)
    else:
        records[index] = replacement
    stored = explicit_binding("acme-bakery", "acme-website-2026", clock=clock)
    got = resolve(stored, projects=projects, records=records)
    assert got.state == "invalid_binding" and got.reason == reason
    assert got.client_id is None and got.project_id is None


def test_explicit_project_whose_client_became_inactive_is_invalid():
    projects, records = world()
    records[0] = client("acme-bakery", status="paused")
    stored = explicit_binding("acme-bakery", "acme-website-2026", clock=clock)
    got = resolve(stored, projects=projects, records=records)
    assert got.state == "invalid_binding" and got.reason == "record_inactive"


def test_explicit_binding_never_falls_back_to_cwd_when_invalid():
    projects, records = world()
    records[0] = client("acme-bakery", status="paused")
    got = resolve(explicit_binding("acme-bakery", clock=clock), cwd="/work/riverbend",
                  projects=projects, records=records)
    assert got.state == "invalid_binding"
    assert got.client_id is None


def test_a_duplicated_id_never_selects_a_record():
    projects, records = world(client("acme-bakery", "Acme Bakery Copy"))
    got = resolve(explicit_binding("acme-bakery", clock=clock), projects=projects, records=records)
    assert got.state == "invalid_binding" and got.reason == "duplicate_id"


def test_a_client_id_pointing_at_a_project_record_is_invalid():
    stored = explicit_binding("riverbend-recall", clock=clock)
    got = resolve(stored)
    assert got.state == "invalid_binding" and got.reason == "kind_mismatch"


def test_a_project_id_pointing_at_a_client_record_is_invalid():
    stored = explicit_binding("acme-bakery", "riverbend-dental", clock=clock)
    got = resolve(stored)
    assert got.state == "invalid_binding" and got.reason == "kind_mismatch"


@pytest.mark.parametrize(
    "stored",
    [
        "acme-bakery",
        42,
        ["explicit"],
        {"mode": "sideways"},
        {"mode": "explicit"},
        {"mode": "explicit", "client": ""},
        {"mode": "explicit", "client": 7},
        {"mode": "explicit", "client": "acme-bakery", "project": 7},
        {},
    ],
)
def test_unreadable_stored_state_is_invalid_not_auto(stored):
    got = resolve(stored, cwd="/work/acme")
    assert got.state == "invalid_binding"
    assert got.reason == "stored_unreadable"
    assert got.client_id is None


def test_explicit_evidence_survives_missing_or_garbled_time():
    base = {"mode": "explicit", "client": "acme-bakery"}
    assert resolve(base).evidence == "explicit"
    assert resolve({**base, "at": "not a time"}).evidence == "explicit"
    assert resolve({**base, "at": "2026-11-07T09:05:00+00:00"}).evidence == "explicit 09:05"


def test_explicit_binding_stamps_time_from_the_injected_clock():
    stored = explicit_binding("acme-bakery", "acme-website-2026", clock=clock)
    assert stored == {
        "mode": "explicit",
        "client": "acme-bakery",
        "project": "acme-website-2026",
        "at": "2026-11-07T14:02:00-06:00",
    }
    assert explicit_binding("acme-bakery", clock=clock)["project"] is None


def test_explicit_binding_rejects_a_naive_clock_and_a_blank_client():
    with pytest.raises(EngagementsError) as exc:
        explicit_binding("acme-bakery", clock=lambda: dt.datetime(2026, 11, 7, 14, 2))
    assert exc.value.code == "invalid_op"
    with pytest.raises(EngagementsError) as exc:
        explicit_binding("", clock=clock)
    assert exc.value.code == "invalid_op"


# --- spec section 9 step 4 layout ---------------------------------------------------


@pytest.fixture
def layout(tmp_path):
    home = tmp_path / "home"
    for rel in ("clients/acme/website", "clients/shared", "clients/riverbend/recall"):
        (home / rel).mkdir(parents=True)
    records = [client("acme-bakery", "Acme Bakery"), client("riverbend-dental", "Riverbend Dental")]
    projects = []
    for path in sorted(FIXTURES.glob("*/Projects/*.md")):
        parsed = parse(path.read_bytes())
        projects.append(
            {
                "id": parsed.id,
                "kind": "project",
                "name": parsed.name,
                "status": parsed.properties["status"],
                "client": parsed.properties["client"],
                "locations": parsed.properties["locations"],
                "valid": not parsed.defects,
            }
        )
    return home, projects, records + projects


def at(layout, rel, **kw):
    home, projects, records = layout
    cwd = home / rel
    return resolve_binding(None, projects, records, str(cwd), "cli", home=str(home), os_name=os.name, **kw)


def test_section_9_step_4_acme_folder_binds_acme(layout):
    got = at(layout, "clients/acme/website")
    assert got.state == "bound" and got.client_id == "acme-bakery"
    assert got.project_id == "acme-website-2026"
    assert at(layout, "clients/acme/website").client_name == "Acme Bakery"


def test_section_9_step_4_riverbend_campaign_folder_binds_riverbend(layout):
    got = at(layout, "clients/riverbend/recall")
    assert got.state == "bound" and got.client_id == "riverbend-dental"
    assert got.project_id == "riverbend-recall-campaign"


def test_section_9_step_4_shared_folder_is_ambiguous_with_nothing_bound(layout):
    got = at(layout, "clients/shared")
    assert got.state == "ambiguous"
    assert [c["id"] for c in got.candidates] == ["acme-website-2026", "riverbend-brand-assets"]
    assert got.client_id is None and got.project_id is None


def test_section_9_step_4_scratch_parent_is_unbound(layout):
    got = at(layout, "clients")
    assert got.state == "unbound" and got.client_id is None


# --- state keys and persistence (A10) ---------------------------------------------------


class Store:
    def __init__(self):
        self.data = {}
        self.writes = []

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value):
        self.writes.append(key)
        self.data[key] = value


def test_binding_key_format():
    assert binding_key("lib-1", "sess-9") == "binding:lib-1:sess-9"


@pytest.mark.parametrize(("lib", "sess", "code"), [("lib", "", "no_session"), ("lib", None, "no_session"),
                                                   ("", "s", "library_missing"), (None, "s", "library_missing")])
def test_binding_key_needs_both_ids(lib, sess, code):
    with pytest.raises(EngagementsError) as exc:
        binding_key(lib, sess)
    assert exc.value.code == code


def test_load_reads_null_and_absent_as_auto():
    store = Store()
    assert load_binding(store.get, "lib", "s1") == {"mode": "auto"}
    store.data["binding:lib:s1"] = None
    assert load_binding(store.get, "lib", "s1") == {"mode": "auto"}


def test_two_sessions_hold_different_bindings_without_touching_each_other():
    store = Store()
    a = explicit_binding("acme-bakery", clock=clock)
    b = explicit_binding("riverbend-dental", "riverbend-recall", clock=clock)
    store_binding(store.set, "lib", "sess-a", a)
    store_binding(store.set, "lib", "sess-b", b)
    assert store.writes == ["binding:lib:sess-a", "binding:lib:sess-b"]
    assert load_binding(store.get, "lib", "sess-a") == a
    assert load_binding(store.get, "lib", "sess-b") == b
    before = dict(store.data)
    store_binding(store.set, "lib", "sess-a", {"mode": "off"})
    assert store.data["binding:lib:sess-b"] == before["binding:lib:sess-b"]
    assert store.writes[-1] == "binding:lib:sess-a"
    assert load_binding(store.get, "lib", "sess-b") == b


def test_one_session_id_in_two_libraries_does_not_collide():
    store = Store()
    store_binding(store.set, "lib-1", "s", {"mode": "off"})
    assert load_binding(store.get, "lib-2", "s") == {"mode": "auto"}


def test_clear_binding_sets_the_key_to_null_which_reads_as_auto():
    store = Store()
    store_binding(store.set, "lib", "s", {"mode": "off"})
    clear_binding(store.set, "lib", "s")
    assert store.data["binding:lib:s"] is None
    assert load_binding(store.get, "lib", "s") == {"mode": "auto"}


def test_store_binding_validates_before_writing_and_copies():
    store = Store()
    with pytest.raises(EngagementsError) as exc:
        store_binding(store.set, "lib", "s", {"mode": "sideways"})
    assert exc.value.code == "invalid_op"
    assert store.writes == []
    value = explicit_binding("acme-bakery", clock=clock)
    store_binding(store.set, "lib", "s", value)
    value["client"] = "mutated"
    assert store.data["binding:lib:s"]["client"] == "acme-bakery"


def test_state_store_oserrors_become_io_error():
    def boom(*_a):
        raise OSError("disk on fire /secret/path")

    with pytest.raises(EngagementsError) as exc:
        load_binding(boom, "lib", "s")
    assert exc.value.code == "io_error"
    assert "secret" not in exc.value.message
    with pytest.raises(EngagementsError) as exc:
        store_binding(boom, "lib", "s", {"mode": "off"})
    assert exc.value.code == "io_error"


# --- compaction migration --------------------------------------------------------------


def test_migrate_copies_the_binding_when_ids_differ():
    store = Store()
    value = explicit_binding("acme-bakery", "acme-website-2026", clock=clock)
    store_binding(store.set, "lib", "old", value)
    store.writes.clear()
    assert migrate(store.get, store.set, "lib", "old", "new") is True
    assert load_binding(store.get, "lib", "new") == value
    assert store.writes == ["binding:lib:new"]
    assert load_binding(store.get, "lib", "old") == value


@pytest.mark.parametrize("old", ["", None, "same"])
def test_migrate_does_nothing_without_a_distinct_old_id(old):
    store = Store()
    store_binding(store.set, "lib", "same", {"mode": "off"})
    store.writes.clear()
    assert migrate(store.get, store.set, "lib", old, "same") is False
    assert store.writes == []


def test_migrate_does_nothing_without_a_new_id_or_an_old_binding():
    store = Store()
    store_binding(store.set, "lib", "old", {"mode": "off"})
    store.writes.clear()
    assert migrate(store.get, store.set, "lib", "old", "") is False
    assert migrate(store.get, store.set, "lib", "never-bound", "new") is False
    store.data["binding:lib:nulled"] = None
    assert migrate(store.get, store.set, "lib", "nulled", "new") is False
    assert store.writes == []


def test_migrate_keeps_an_existing_binding_on_the_new_session():
    # SPEC-QUESTION: spec 6.4 does not say what migrate does when the new session
    # id already holds a non-null binding. Conservative reading: keep it.
    store = Store()
    store_binding(store.set, "lib", "old", explicit_binding("acme-bakery", clock=clock))
    mine = explicit_binding("riverbend-dental", clock=clock)
    store_binding(store.set, "lib", "new", mine)
    assert migrate(store.get, store.set, "lib", "old", "new") is False
    assert load_binding(store.get, "lib", "new") == mine
