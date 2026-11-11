"""The two Clients front ends, executed rather than searched for phrases.

Each test loads the real ``desktop/plugin.js`` or ``dashboard/dist/index.js`` into Node with a
small hooks runtime and mocked SDK components (``tests/js/component_probe.mjs``), feeds it the
real backend envelopes, and drives it the way a person would: type a search, select a row, turn
on privacy, wait for a deferred response. The screenshots taken by hand become
assertions here; no browser or Electron session is involved.

Skipped when Node is not installed; a run that sets ``ENGAGEMENTS_REQUIRE_NODE=1`` fails at
collection instead, so CI cannot pass with these tests quietly absent.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from engagements import dashboard_data
from engagements.library import open_library_readonly
from test_dashboard import make_host, rows
from tools_support import copy_library

ROOT = Path(__file__).resolve().parent.parent
PROBE = ROOT / "tests" / "js" / "component_probe.mjs"
WEB_API = "/api/plugins/engagements"
NODE_ENV = "ENGAGEMENTS_REQUIRE_NODE"


def require_node(which, demanded):
    """The Node executable, or a collection error when the environment demands one.

    A missing Node skips these tests only when nothing demands it. A CI job that installs
    Node sets the switch, so a provisioning failure stops the run instead of skipping it.
    """
    if which is None and demanded:
        raise RuntimeError(
            f"node is required to run the front end tests ({NODE_ENV} is set) but was not found on PATH"
        )
    return which


NODE = require_node(shutil.which("node"), os.environ.get(NODE_ENV, "") == "1")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

FRONT_ENDS = {"desktop": ROOT / "desktop" / "plugin.js", "web": ROOT / "dashboard" / "dist" / "index.js"}


def run(frontend, steps, tmp_path, *, privacy=False, session=None, draft_works=None, clipboard_fail=None):
    """Drive one front end and return one snapshot per step."""
    scenario = {"frontend": frontend, "file": str(FRONT_ENDS[frontend]), "steps": steps, "privacy": privacy}
    if session is not None:
        scenario["session"] = session
    if draft_works is not None:
        scenario["draftWorks"] = draft_works
    if clipboard_fail is not None:
        scenario["clipboardFail"] = clipboard_fail
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps(scenario), encoding="utf-8")
    # Node writes UTF-8 whatever the platform; without an explicit encoding Windows decodes it
    # with the locale code page and mangles any non-ASCII text on screen, such as "…".
    result = subprocess.run([NODE, str(PROBE), str(path)], capture_output=True, text=True, encoding="utf-8",
                            check=False, timeout=180)
    assert result.returncode == 0, f"the {frontend} front end threw:\n{result.stderr}"
    return json.loads(result.stdout)["snapshots"]


def every_render(snapshot):
    """Each render of a step, including the ones before its effects ran, and the settled view.

    A guard that holds only once effects have run would pass on the settled view alone, while
    an earlier render still carried the old identity.
    """
    return [*snapshot["passes"], snapshot]


def path(frontend, route):
    return route if frontend == "desktop" else WEB_API + route


def select(client_id, name):  # noqa: ARG001 - both front ends tag rows with the id
    """Select a row the way a person does: its row button, found by the id the row carries."""
    return {"op": "select", "id": client_id}


def client_row(client_id, name, *, projects=(), status="active", valid=True):
    return {"id": client_id, "kind": "client", "name": name, "status": status, "valid": valid,
            "path": f"{name}/Client.md", "abs_path": f"/fictional/lib/{name}/Client.md",
            "folder": f"/fictional/lib/{name}", "projects": list(projects)}


def project_row(project_id, name, client, *, status="active", valid=True, locations=()):
    return {"id": project_id, "kind": "project", "name": name, "status": status, "valid": valid,
            "client": client, "path": f"{name}.md", "abs_path": f"/fictional/lib/{name}.md",
            "folder": "/fictional/lib", "locations": list(locations)}


def overview(clients=(), unassigned=(), *, status="ok", message=None, duplicated=(), unreadable=()):
    """The real `/overview` payload shape, including the notice's `unlisted` list.

    The backend names every record it cannot list once in `unlisted`, whatever kept it off the
    list, and keeps the classification in `duplicated` and `unreadable`.
    """
    projects = sum(len(client["projects"]) for client in clients) + len(unassigned)
    notices = [{"id": item.get("id"), "path": item["path"], "reason": item["reason"], "defects": item["defects"]}
               for item in [*duplicated, *unreadable]]
    notices.sort(key=lambda item: item["path"])
    return {"status": status, "message": message, "library_root": "/fictional/lib", "labels": {},
            "generated_at": "2026-11-08T12:00:00-06:00", "clients": list(clients),
            "unassigned": list(unassigned), "unreadable": list(unreadable), "duplicated": list(duplicated),
            "unlisted": notices,
            "counts": {"clients": len(clients), "projects": projects, "active_projects": projects},
            "doctor": {"clean": True, "counts": {"error": 0, "warning": 0}, "checked": len(clients) + projects}}


DUPLICATE_REASON = "Its id is used by more than one record."
NO_ID_REASON = "Its id could not be read."
NO_KIND_REASON = "Its kind could not be read as a client or project."
# What the detail pane says for a selection no notice entry explains, e.g. one deleted since.
NOT_LISTED_GENERIC = "It is not in the latest list of clients and projects."


def unlisted(relative, kind="client", *, record_id="acme-bakery", defects=("duplicate_id",), reason=DUPLICATE_REASON):
    """A record the overview names in its notice instead of listing: a shared id, or an
    unreadable id or kind."""
    return {"id": record_id, "kind": kind, "path": relative, "defects": list(defects), "reason": reason}


def unreadable(relative, *, record_id=None, defects=("duplicate_key",), reason=NO_KIND_REASON):
    """A record with no readable id or kind, which the notice also has to name."""
    return {"id": record_id, "kind": None, "path": relative, "defects": list(defects), "reason": reason}


def record_body(record_id, name, kind="client", *, client=None, sections=(), status="ok", message=None):
    body = {"status": status, "message": message, "library_root": "/fictional/lib", "labels": {},
            "generated_at": "2026-11-08T12:00:00-06:00",
            "id": record_id, "kind": kind, "name": name, "record_status": "active", "valid": True,
            "defects": [], "path": f"{name}.md", "abs_path": f"/fictional/lib/{name}.md",
            "folder": "/fictional/lib", "properties": {},
            "sections": [{"title": title, "text": text} for title, text in sections], "truncated": False}
    if kind == "client":
        body["projects"] = []
    else:
        body["client"] = client or {"id": "acme-bakery", "name": "Acme Bakery", "known": True}
    return body


MISSING = "No library exists at /fictional/acme/library. It is created the first time the agent uses a client tool."
HIDDEN = "Details are hidden while privacy mode is on."


# --- privacy mode on the Clients page -------------------------------------------------------


@pytest.mark.parametrize("frontend", list(FRONT_ENDS))
def test_privacy_hides_a_path_in_the_missing_library_message(frontend, tmp_path):
    """The backend's missing-library sentence carries an absolute path; privacy on must hide it."""
    snapshots = run(frontend, [
        {"op": "render"},
        {"op": "resolve", "path": path(frontend, "/overview"),
         "body": overview(status="missing", message=MISSING)},
        {"op": "togglePrivacy"},
    ], tmp_path)
    assert "/fictional/acme/library" in snapshots[1]["text"], "control: the path is on screen with privacy off"
    for view in every_render(snapshots[2]):
        assert "/fictional/acme/library" not in view["text"]
    assert HIDDEN in snapshots[2]["text"]


@pytest.mark.parametrize("frontend", list(FRONT_ENDS))
def test_privacy_clears_a_search_that_names_a_client(frontend, tmp_path):
    """A name typed before privacy was turned on must not stay in the search field."""
    snapshots = run(frontend, [
        {"op": "render"},
        {"op": "resolve", "path": path(frontend, "/overview"), "body": overview(clients=[client_row("acme-bakery", "Acme Bakery")])},
        {"op": "type", "value": "Acme Bakery"},
        {"op": "togglePrivacy"},
    ], tmp_path)
    assert snapshots[2]["inputs"] == [{"placeholder": "Find a client or project", "value": "Acme Bakery"}]
    for view in every_render(snapshots[3]):
        assert view["inputs"] == [{"placeholder": "Find a client or project", "value": ""}]
        assert "Acme Bakery" not in view["text"]


@pytest.mark.parametrize("frontend", list(FRONT_ENDS))
def test_turning_privacy_off_again_does_not_bring_the_search_back(frontend, tmp_path):
    snapshots = run(frontend, [
        {"op": "render"},
        {"op": "resolve", "path": path(frontend, "/overview"), "body": overview(clients=[client_row("acme-bakery", "Acme Bakery")])},
        {"op": "type", "value": "Acme Bakery"},
        {"op": "togglePrivacy"},
        {"op": "togglePrivacy"},
    ], tmp_path)
    for view in every_render(snapshots[4]):
        assert view["inputs"] == [{"placeholder": "Find a client or project", "value": ""}]


@pytest.mark.parametrize("frontend", list(FRONT_ENDS))
def test_privacy_hides_a_diagnostic_that_names_the_record(frontend, tmp_path):
    """A 404 from the record route quotes the id; privacy on replaces the sentence."""
    snapshots = run(frontend, [
        {"op": "render"},
        {"op": "resolve", "path": path(frontend, "/overview"), "body": overview(clients=[client_row("acme-bakery", "Acme Bakery")])},
        select("acme-bakery", "Acme Bakery"),
        {"op": "reject", "path": path(frontend, "/record/acme-bakery"),
         "message": "No client or project with id 'acme-bakery'."},
        {"op": "togglePrivacy"},
    ], tmp_path)
    assert "No client or project with id 'acme-bakery'." in snapshots[3]["text"]
    for view in every_render(snapshots[4]):
        assert "acme-bakery" not in view["text"]


# --- a new selection must not show the old identity -----------------------------------------


@pytest.mark.parametrize("frontend", list(FRONT_ENDS))
def test_a_new_selection_shows_nothing_from_the_previous_record_until_it_arrives(frontend, tmp_path):
    acme = record_body("acme-bakery", "Acme Bakery", sections=[("Summary", "ACME-SUMMARY-MARKER")])
    riverbend = record_body("riverbend-dental", "Riverbend Dental", sections=[("Summary", "RIVERBEND-SUMMARY-MARKER")])
    snapshots = run(frontend, [
        {"op": "render"},
        {"op": "resolve", "path": path(frontend, "/overview"),
         "body": overview(clients=[client_row("acme-bakery", "Acme Bakery"),
                                   client_row("riverbend-dental", "Riverbend Dental")])},
        select("acme-bakery", "Acme Bakery"),
        {"op": "resolve", "path": path(frontend, "/record/acme-bakery"), "body": acme},
        select("riverbend-dental", "Riverbend Dental"),
        {"op": "resolve", "path": path(frontend, "/record/riverbend-dental"), "body": riverbend},
    ], tmp_path)
    assert "ACME-SUMMARY-MARKER" in snapshots[3]["text"], "control: the first record is shown"
    # The second request is still in flight: none of the first record's content or actions may render.
    for view in every_render(snapshots[4]):
        assert "ACME-SUMMARY-MARKER" not in view["text"]
        assert "/fictional/lib/Acme Bakery.md" not in view["text"]
        assert "Start a chat" not in view["text"], "no action may act on the previous record"
    assert "Loading" in snapshots[4]["text"]
    assert "RIVERBEND-SUMMARY-MARKER" in snapshots[5]["text"]


def test_the_binding_chip_shows_nothing_for_the_previous_session(tmp_path):
    """Desktop's chip must not keep identifying the old chat while the new one is read."""
    acme = {"status": "ok", "message": None, "library_root": "/fictional/lib", "labels": {},
            "generated_at": "2026-11-08T12:00:00-06:00", "session": True, "mode": "explicit",
            "state": "bound", "client": {"id": "acme-bakery", "name": "Acme Bakery"},
            "project": None, "automatic": False}
    snapshots = run("desktop", [
        {"op": "render"},
        {"op": "atom", "name": "focusedStoredSessionId", "value": "s-acme"},
        {"op": "resolve", "path": "/session?id=s-acme", "body": acme},
        {"op": "atom", "name": "focusedStoredSessionId", "value": "s-other"},
        {"op": "snapshot"},
    ], tmp_path)
    assert snapshots[2]["chips"] and "Acme Bakery" in snapshots[2]["chips"][0]["text"]
    # The new session's answer is still pending, so the chip renders nothing at all.
    for view in every_render(snapshots[3]):
        assert view["chips"] == []
    assert snapshots[4]["chips"] == []


def test_the_binding_chip_reads_no_client_for_an_unlinked_chat(tmp_path):
    """The README and user guide promise this state, so it is exercised rather than assumed."""
    unbound = {"status": "ok", "message": None, "library_root": "/fictional/lib", "labels": {},
               "generated_at": "2026-11-08T12:00:00-06:00", "session": True, "mode": "auto",
               "state": "unbound", "client": None, "project": None, "automatic": False}
    snapshots = run("desktop", [
        {"op": "render"},
        {"op": "atom", "name": "focusedStoredSessionId", "value": "s-fresh"},
        {"op": "resolve", "path": "/session?id=s-fresh", "body": unbound},
    ], tmp_path)
    assert snapshots[2]["chips"] == [{"id": "", "text": "No client"}]


# --- an unavailable library in a record response --------------------------------------------


@pytest.mark.parametrize("frontend", list(FRONT_ENDS))
@pytest.mark.parametrize("privacy", [False, True], ids=["privacy-off", "privacy-on"])
def test_an_unavailable_record_response_is_explained_not_thrown(frontend, privacy, tmp_path):
    snapshots = run(frontend, [
        {"op": "render"},
        {"op": "resolve", "path": path(frontend, "/overview"), "body": overview(clients=[client_row("acme-bakery", "Acme Bakery")])},
        select("acme-bakery", "Acme Bakery"),
        {"op": "resolve", "path": path(frontend, "/record/acme-bakery"),
         "body": overview(status="missing", message=MISSING)},
    ], tmp_path, privacy=privacy)
    text = snapshots[3]["text"]
    assert "No client library yet" in text
    if privacy:
        assert "/fictional/acme/library" not in text and HIDDEN in text
    else:
        assert "/fictional/acme/library" in text


# --- projects with no client ----------------------------------------------------------------


@pytest.mark.parametrize("frontend", list(FRONT_ENDS))
def test_unassigned_projects_are_listed_and_cannot_start_a_chat(frontend, tmp_path):
    orphan = project_row("maple-window", "Maple Street Window", "maple-street-florist")
    snapshots = run(frontend, [
        {"op": "render"},
        {"op": "resolve", "path": path(frontend, "/overview"),
         "body": overview(clients=[client_row("acme-bakery", "Acme Bakery")], unassigned=[orphan])},
        select("maple-window", "Maple Street Window"),
        {"op": "resolve", "path": path(frontend, "/record/maple-window"),
         "body": record_body("maple-window", "Maple Street Window", "project",
                             client={"id": "maple-street-florist", "name": None, "known": False})},
    ], tmp_path)
    listing = snapshots[1]["text"]
    assert "Needs a client" in listing
    assert "Maple Street Window" in listing
    assert "1 clients, 1 projects, 1 active" in listing, "the header count agrees with the rows listed"
    detail = snapshots[3]["text"]
    assert "(unknown client)" in detail
    assert "/client" not in detail
    if frontend == "desktop":
        assert "Start a chat" not in detail, "no chat action without a client to bind to"


@pytest.mark.parametrize("frontend", list(FRONT_ENDS))
def test_unassigned_projects_are_masked_under_privacy(frontend, tmp_path):
    orphan = project_row("maple-window", "Maple Street Window", "maple-street-florist")
    snapshots = run(frontend, [
        {"op": "render"},
        {"op": "resolve", "path": path(frontend, "/overview"),
         "body": overview(clients=[client_row("acme-bakery", "Acme Bakery")], unassigned=[orphan])},
    ], tmp_path, privacy=True)
    text = snapshots[1]["text"]
    assert "Needs a client" in text
    assert "Maple Street Window" not in text
    assert "M. S. W." in text


# --- duplicate ids: the records the page cannot list ---------------------------------------------

CONFLICT = (
    "The id 'acme-bakery' is used by more than one record, so this page cannot tell them apart. "
    "Give each record its own id, then try again. hermes engagements doctor lists the files that share one."
)


@pytest.mark.parametrize("frontend", list(FRONT_ENDS))
def test_the_page_names_every_record_it_cannot_list(frontend, tmp_path):
    """A record left off the list is named with its path, the reason for it, and where to look next."""
    snapshots = run(frontend, [
        {"op": "render"},
        {"op": "resolve", "path": path(frontend, "/overview"),
         "body": overview(clients=[client_row("riverbend-dental", "Riverbend Dental")],
                          duplicated=[unlisted("Acme Bakery/Client.md"), unlisted("Zebra Duplicate/Client.md")])},
    ], tmp_path)
    text = snapshots[-1]["text"]
    assert "2 records are not listed" in text
    assert "Acme Bakery/Client.md" in text and "Zebra Duplicate/Client.md" in text
    assert text.count(DUPLICATE_REASON) == 2, "each named record carries its own reason"
    assert "hermes engagements doctor" in text
    assert "Riverbend Dental" in text, "control: the records it can list are still listed"
    assert snapshots[-1]["ids"] == ["riverbend-dental"], "an unlisted record is never offered as a row"


@pytest.mark.parametrize("frontend", list(FRONT_ENDS))
def test_the_unlisted_notice_hides_its_paths_under_privacy(frontend, tmp_path):
    snapshots = run(frontend, [
        {"op": "render"},
        {"op": "resolve", "path": path(frontend, "/overview"),
         "body": overview(clients=[client_row("riverbend-dental", "Riverbend Dental")],
                          duplicated=[unlisted("Acme Bakery/Client.md"), unlisted("Zebra Duplicate/Client.md")])},
    ], tmp_path, privacy=True)
    text = snapshots[-1]["text"]
    assert "2 records are not listed" in text
    assert "Acme Bakery/Client.md" not in text and "Zebra" not in text and "Acme" not in text
    assert text.count(DUPLICATE_REASON) == 2, "the reason stays readable; only the path is hidden"
    assert "hermes engagements doctor" in text


@pytest.mark.parametrize("frontend", list(FRONT_ENDS))
def test_the_notice_names_a_plain_unreadable_record_with_its_own_reason(frontend, tmp_path):
    """A record with no readable id or kind is not a duplicate, so it gets a different sentence."""
    snapshots = run(frontend, [
        {"op": "render"},
        {"op": "resolve", "path": path(frontend, "/overview"),
         "body": overview(clients=[client_row("riverbend-dental", "Riverbend Dental")],
                          unreadable=[unreadable("Maple Florist/Broken.md")])},
    ], tmp_path)
    text = snapshots[-1]["text"]
    assert "1 record is not listed" in text
    assert "Maple Florist/Broken.md" in text
    assert NO_KIND_REASON in text
    assert DUPLICATE_REASON not in text, "an unreadable kind is not described as a shared id"


@pytest.mark.parametrize("frontend", list(FRONT_ENDS))
def test_the_page_says_nothing_about_unlisted_records_when_there_are_none(frontend, tmp_path):
    snapshots = run(frontend, [
        {"op": "render"},
        {"op": "resolve", "path": path(frontend, "/overview"),
         "body": overview(clients=[client_row("acme-bakery", "Acme Bakery")])},
    ], tmp_path)
    assert "not listed" not in snapshots[-1]["text"]


@pytest.mark.parametrize("frontend", list(FRONT_ENDS))
@pytest.mark.parametrize("privacy", [False, True], ids=["privacy-off", "privacy-on"])
def test_a_shared_id_from_an_older_list_shows_the_error_and_no_record(frontend, privacy, tmp_path):
    """The list the page holds can predate a duplicate: the detail then opens neither record."""
    riverbend = record_body("riverbend-dental", "Riverbend Dental", sections=[("Summary", "RIVERBEND-SUMMARY-MARKER")])
    snapshots = run(frontend, [
        {"op": "render"},
        {"op": "resolve", "path": path(frontend, "/overview"),
         "body": overview(clients=[client_row("acme-bakery", "Acme Bakery"),
                                   client_row("riverbend-dental", "Riverbend Dental")])},
        select("riverbend-dental", "Riverbend Dental"),
        {"op": "resolve", "path": path(frontend, "/record/riverbend-dental"), "body": riverbend},
        select("acme-bakery", "Acme Bakery"),
        {"op": "reject", "path": path(frontend, "/record/acme-bakery"), "message": CONFLICT},
    ], tmp_path, privacy=privacy)
    control = snapshots[3]["text"]
    assert "Start a chat" in control, "control: a record was on screen"
    if not privacy:
        assert "RIVERBEND-SUMMARY-MARKER" in control
    for view in every_render(snapshots[4]):
        assert "RIVERBEND-SUMMARY-MARKER" not in view["text"], "the new selection shows no other record"
        assert "Start a chat" not in view["text"], "no action may act on another record"
    text = snapshots[5]["text"]
    assert "RIVERBEND-SUMMARY-MARKER" not in text and "/fictional/lib/Riverbend Dental.md" not in text
    assert "Start a chat" not in text, "no action may act on either record under this id"
    if privacy:
        assert "acme-bakery" not in text and HIDDEN in text
    else:
        assert "used by more than one record" in text


# --- every record the page cannot list is named exactly once -----------------------------------
#
# The payload is the real `/overview` from a real library, not a hand-written envelope, so the
# invariant is checked against what the backend actually produces: one entry per discovery entry,
# either a row or named once in the notice with a reason, whatever else was wrong with the file.

NOTICE_TEMPLATE = "---\n{frontmatter}---\n\n## Summary\nFictional body.\n"


def write_record(root, relative, frontmatter):
    target = Path(root) / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(NOTICE_TEMPLATE.format(frontmatter=frontmatter), encoding="utf-8")
    return target


def record_frontmatter(kind="client", record_id="shared-id", name="Fictional Record", client=None):
    lines = [f"engagements: {kind}" if kind else "", f"id: {record_id}" if record_id else ""]
    if client:
        lines.append(f"client: {client}")
    lines += [f"name: {name}", "status: active", "updated: 2026-11-05"]
    return "\n".join(line for line in lines if line) + "\n"


NOTICE_LIBRARIES = {
    # A shared id on one record whose kind cannot be read: both are duplicates, and the notice
    # used to name only the readable one.
    "duplicate-with-a-malformed-kind": [
        ("Collision/Client.md", record_frontmatter(name="Collision Client")),
        ("Collision/Repeated.md", "engagements: client\nengagements: project\nid: shared-id\n"
                                  "name: Repeated Marker\nstatus: active\nupdated: 2026-11-05\n"),
    ],
    # A missing id on one side: the other file cannot carry duplicate_id, so it is a plain
    # unreadable record and still has to be named.
    "duplicate-with-a-missing-id": [
        ("Collision/Client.md", record_frontmatter(name="Collision Client")),
        ("Collision/NoId.md", "engagements: client\nid: shared-id\nid: repeated-id\n"
                              "name: No Readable Id\nstatus: active\nupdated: 2026-11-05\n"),
    ],
    # A record with no readable kind and an id of its own: no duplicate involved.
    "plain-unreadable": [
        ("Broken/Repeated.md", "engagements: client\nengagements: project\nid: broken-only\n"
                               "name: Repeated Marker\nstatus: active\nupdated: 2026-11-05\n"),
    ],
    # Two ordinary records under one id.
    "two-duplicates": [
        ("Collision/Client.md", record_frontmatter(name="Collision Client")),
        ("Collision/Zebra.md", record_frontmatter(name="Collision Zebra")),
    ],
    # Nothing to report.
    "clean-library": [],
}


def notice_library(case, tmp_path):
    root = copy_library(tmp_path)
    for relative, frontmatter in NOTICE_LIBRARIES[case]:
        write_record(root, relative, frontmatter)
    return root


@pytest.mark.parametrize("frontend", list(FRONT_ENDS))
@pytest.mark.parametrize("case", list(NOTICE_LIBRARIES))
def test_the_notice_names_every_record_the_page_cannot_list_exactly_once(frontend, case, tmp_path):
    root = notice_library(case, tmp_path)
    host, _ = make_host({"library_root": str(root)})
    data = dashboard_data.overview(host)
    entries = open_library_readonly(root).discover().entries

    listed = {row["path"] for row in rows(data)}
    named = [item["path"] for item in data["unlisted"]]
    assert len(named) == len(set(named)), "a record is never named twice"
    assert not listed & set(named), "a record is either a row or named, never both"
    assert listed | set(named) == {entry.relpath for entry in entries}, "no record is left out"
    assert data["duplicated"] + data["unreadable"] or not named, "the notice keeps its classification"

    snapshots = run(frontend, [
        {"op": "render"},
        {"op": "resolve", "path": path(frontend, "/overview"), "body": data},
    ], tmp_path)
    text = snapshots[-1]["text"]
    if not named:
        assert "not listed" not in text, "a clean library shows no notice"
    else:
        heading = "1 record is not listed" if len(named) == 1 else f"{len(named)} records are not listed"
        assert heading in text, "the heading counts the records it names"
        for item in data["unlisted"]:
            assert text.count(item["path"]) == 1, item["path"]
            assert item["reason"] in text
    assert set(snapshots[-1]["ids"]) == {row["id"] for row in rows(data)}, (
        "the notice never offers a record the list cannot open")


@pytest.mark.parametrize("frontend", list(FRONT_ENDS))
@pytest.mark.parametrize("order", ["overview-first", "detail-late"])
def test_a_selection_the_latest_list_no_longer_lists_shows_no_record(frontend, order, tmp_path):
    """A record the page already opened stops being listed; no cached detail or late answer stays.

    Both orders are covered because the two requests are independent: an overview that reports
    the id as duplicated arrives either on its own or ahead of a detail response already in flight.
    """
    stale = record_body("acme-bakery", "Acme Bakery", sections=[("Summary", "OLD-DETAIL-MARKER")])
    later = overview(clients=[], duplicated=[unlisted("Acme Bakery/Client.md"),
                                            unlisted("Zebra Duplicate/Client.md")])
    steps = [
        {"op": "render"},
        {"op": "resolve", "path": path(frontend, "/overview"),
         "body": overview(clients=[client_row("acme-bakery", "Acme Bakery")])},
        select("acme-bakery", "Acme Bakery"),
        {"op": "resolve", "path": path(frontend, "/record/acme-bakery"), "body": stale},
        {"op": "click", "text": "Refresh"},
        {"op": "resolve", "path": path(frontend, "/overview"), "body": later},
    ]
    if order == "detail-late":
        steps.append({"op": "resolve", "path": path(frontend, "/record/acme-bakery"), "body": stale})
    snapshots = run(frontend, steps, tmp_path)

    control = snapshots[3]["text"]
    assert "OLD-DETAIL-MARKER" in control, "control: the record was on screen before the refresh"
    assert "Start a chat" in control or "/client acme-bakery" in control, "control: its action was offered"
    for view in snapshots[5:]:
        assert "OLD-DETAIL-MARKER" not in view["text"], "a record the latest list does not list stays off screen"
        assert "Start a chat" not in view["text"] and "/client acme-bakery" not in view["text"], (
            "no action may act on a record the list does not offer")
        assert "This record is not listed" in view["text"]
        # The notice names both records, so the selected id's own reason appears a third time
        # only if the pane repeats it instead of falling back to a generic sentence.
        assert view["text"].count(DUPLICATE_REASON) == 3, "the pane names why the record is not listed"
        assert NOT_LISTED_GENERIC not in view["text"]
    assert "2 records are not listed" in snapshots[-1]["text"], "the notice explains why"


@pytest.mark.parametrize("frontend", list(FRONT_ENDS))
def test_a_selection_missing_from_the_latest_list_shows_a_not_listed_state(frontend, tmp_path):
    """An id no notice entry explains, because the record is gone, still gets a state of its own."""
    stale = record_body("acme-bakery", "Acme Bakery", sections=[("Summary", "OLD-DETAIL-MARKER")])
    snapshots = run(frontend, [
        {"op": "render"},
        {"op": "resolve", "path": path(frontend, "/overview"),
         "body": overview(clients=[client_row("acme-bakery", "Acme Bakery")])},
        select("acme-bakery", "Acme Bakery"),
        {"op": "resolve", "path": path(frontend, "/record/acme-bakery"), "body": stale},
        {"op": "click", "text": "Refresh"},
        {"op": "resolve", "path": path(frontend, "/overview"), "body": overview(clients=[])},
    ], tmp_path)
    text = snapshots[-1]["text"]
    assert "This record is not listed" in text
    assert NOT_LISTED_GENERIC in text
    assert "OLD-DETAIL-MARKER" not in text, "the cached record is not left on screen"
    assert "Start a chat" not in text and "/client acme-bakery" not in text, "and no action is offered"


# --- privacy is remembered per window -------------------------------------------------------


def test_web_privacy_is_kept_per_window_not_per_origin(tmp_path):
    snapshots = run("web", [
        {"op": "render"},
        {"op": "togglePrivacy"},
    ], tmp_path)
    assert snapshots[1]["session"].get("hermes.plugin.engagements.privacy") == "true"
    assert "hermes.plugin.engagements.privacy" not in snapshots[1]["local"]


# --- starter prompts on an empty Clients page (decision D12) ---------------------------------------------
#
# The prompts are the product surface here, so they are pinned word for word exactly as a user
# would receive them, in both front ends. The rest of each test drives a real button.

SUGGEST_PROMPT = (
    "Look through my past Hermes chats for clients or recurring projects I have worked on. "
    "Use the engagements:suggest-clients skill. Suggest up to five worth adding, each with one "
    "line on why and which chats it came from. Do not save anything yet; let me pick. If you find "
    "none, help me add one client instead."
)
ADD_PROMPT = (
    "Help me add a client to Engagements. Use the engagements:onboard-client skill. "
    "Ask me only what you need."
)
STARTER_BUTTONS = ("Find clients in my past chats", "Add a client")
UNAVAILABLE_TEXT = "The library folder cannot be read."


def starter_labels(text):
    return [label for label in STARTER_BUTTONS if label in text]


@pytest.mark.parametrize("frontend", list(FRONT_ENDS))
@pytest.mark.parametrize("state", ["missing", "empty"])
def test_the_starter_prompts_show_when_there_is_nothing_to_list(frontend, state, tmp_path):
    """A library that does not exist yet, or one with no records at all, offers both starters."""
    body = overview(status="missing", message=MISSING) if state == "missing" else overview()
    snapshots = run(frontend, [
        {"op": "render"},
        {"op": "resolve", "path": path(frontend, "/overview"), "body": body},
    ], tmp_path)
    text = snapshots[-1]["text"]
    assert starter_labels(text) == list(STARTER_BUTTONS)
    assert "Find clients from chats you have already had" in text
    assert snapshots[-1]["inputs"] == [], "no search field to search a library with nothing in it"
    if state == "missing":
        assert "/fictional/acme/library" in text, "the init --root hint stays as secondary text"


def hiding_body(which):
    if which == "has-a-client":
        return overview(clients=[client_row("acme-bakery", "Acme Bakery")])
    if which == "unassigned-project":
        return overview(unassigned=[project_row("maple-window", "Maple Street Window", "maple-street-florist")])
    return overview(status="unavailable", message=UNAVAILABLE_TEXT)


@pytest.mark.parametrize("frontend", list(FRONT_ENDS))
@pytest.mark.parametrize("which", ["has-a-client", "unassigned-project", "unavailable"])
def test_the_starter_prompts_hide_when_they_do_not_apply(frontend, which, tmp_path):
    """A populated library, or one that cannot be written to, keeps today's message and no starters.

    A populated page has its own "Add a client" under the search (decision D13), so the starter
    block is recognised by its other button and its story line rather than by that label.
    """
    snapshots = run(frontend, [
        {"op": "render"},
        {"op": "resolve", "path": path(frontend, "/overview"), "body": hiding_body(which)},
    ], tmp_path)
    text = snapshots[-1]["text"]
    assert "Find clients in my past chats" not in text
    assert "Find clients from chats you have already had" not in text
    if which == "unavailable":
        assert starter_labels(text) == []
        assert UNAVAILABLE_TEXT in snapshots[-1]["text"], "the error state keeps its own message"
        assert snapshots[-1]["inputs"] == [], "nothing to search when the library cannot be read"
    else:
        assert snapshots[-1]["inputs"] == [{"placeholder": "Find a client or project", "value": ""}], (
            "a populated list keeps its search field"
        )


@pytest.mark.parametrize("frontend", list(FRONT_ENDS))
def test_the_starter_prompts_hide_when_the_overview_fails(frontend, tmp_path):
    snapshots = run(frontend, [
        {"op": "render"},
        {"op": "reject", "path": path(frontend, "/overview"), "message": "The Engagements backend is not running."},
    ], tmp_path)
    assert starter_labels(snapshots[-1]["text"]) == []


@pytest.mark.parametrize("frontend", list(FRONT_ENDS))
def test_privacy_keeps_the_starter_visible_because_the_prompts_hold_no_data(frontend, tmp_path):
    snapshots = run(frontend, [
        {"op": "render"},
        {"op": "resolve", "path": path(frontend, "/overview"), "body": overview(status="missing", message=MISSING)},
        {"op": "togglePrivacy"},
    ], tmp_path)
    for view in every_render(snapshots[2]):
        assert starter_labels(view["text"]) == list(STARTER_BUTTONS)
        assert "/fictional/acme/library" not in view["text"]


@pytest.mark.parametrize("label,prompt", [
    ("Find clients in my past chats", SUGGEST_PROMPT),
    ("Add a client", ADD_PROMPT),
])
def test_desktop_drafts_each_starter_prompt_in_a_new_chat_and_never_sends_it(label, prompt, tmp_path):
    snapshots = run("desktop", [
        {"op": "render"},
        {"op": "resolve", "path": "/overview", "body": overview()},
        {"op": "click", "text": label},
    ], tmp_path, draft_works=True)
    calls = snapshots[-1]["calls"]
    assert calls["newChat"] == [True], "the button opens a new chat"
    assert calls["setDraft"] == [{"target": "new", "draft": prompt}]
    assert calls["composerSubmit"] == 0, "the user presses Enter; the button never sends"
    assert "Press Enter" in snapshots[-1]["notifications"][-1]["message"]


def test_desktop_copies_a_starter_prompt_when_no_composer_takes_the_draft(tmp_path):
    """The same setDraft-then-clipboard fallback "Start a chat" uses, on the starter path."""
    snapshots = run("desktop", [
        {"op": "render"},
        {"op": "resolve", "path": "/overview", "body": overview()},
        {"op": "click", "text": "Find clients in my past chats"},
    ], tmp_path)  # draftWorks defaults false: no mounted composer answers
    calls = snapshots[-1]["calls"]
    assert calls["setDraft"] and all(entry["draft"] == SUGGEST_PROMPT for entry in calls["setDraft"])
    assert calls["clipboard"] == [SUGGEST_PROMPT]
    assert snapshots[-1]["notifications"][-1]["message"] == "Copied the prompt. Paste it into the new chat and press Enter."
    assert calls["composerSubmit"] == 0


def test_desktop_start_a_chat_still_drafts_the_client_command(tmp_path):
    """The shared helper did not regress the action it was extracted from."""
    snapshots = run("desktop", [
        {"op": "render"},
        {"op": "resolve", "path": "/overview", "body": overview(clients=[client_row("acme-bakery", "Acme Bakery")])},
        select("acme-bakery", "Acme Bakery"),
        {"op": "resolve", "path": "/record/acme-bakery", "body": record_body("acme-bakery", "Acme Bakery")},
        {"op": "click", "text": "Start a chat"},
    ], tmp_path, draft_works=True)
    calls = snapshots[-1]["calls"]
    assert calls["setDraft"] == [{"target": "new", "draft": "/client acme-bakery"}]
    assert snapshots[-1]["notifications"][-1]["message"] == "New chat ready. Press Enter to send /client acme-bakery."
    assert calls["composerSubmit"] == 0


@pytest.mark.parametrize("label,prompt", [
    ("Find clients in my past chats", SUGGEST_PROMPT),
    ("Add a client", ADD_PROMPT),
])
def test_the_web_tab_copies_each_starter_prompt_and_shows_where_to_paste_it(label, prompt, tmp_path):
    snapshots = run("web", [
        {"op": "render"},
        {"op": "resolve", "path": path("web", "/overview"), "body": overview()},
        {"op": "click", "text": label},
    ], tmp_path)
    calls = snapshots[-1]["calls"]
    assert calls["clipboard"] == [prompt]
    text = snapshots[-1]["text"]
    assert prompt in text, "the page shows exactly what it copied"
    assert "Paste it into a new chat" in text
    assert calls["newChat"] == [] and calls["composerSubmit"] == 0, "the web tab has no composer to send with"
    assert snapshots[-1]["pending"] == [], "a copy starts no request"


def test_the_web_tab_still_shows_the_prompt_when_the_clipboard_is_unavailable(tmp_path):
    """An insecure origin has no navigator.clipboard: the prompt is shown to copy by hand."""
    snapshots = run("web", [
        {"op": "render"},
        {"op": "resolve", "path": path("web", "/overview"), "body": overview()},
        {"op": "click", "text": "Find clients in my past chats"},
    ], tmp_path, clipboard_fail=True)
    text = snapshots[-1]["text"]
    assert SUGGEST_PROMPT in text
    assert "Copy this prompt into a new chat." in text


# --- "Add a client" and "Add a project" on a populated page (decision D13) -------------------------
#
# Both only draft or copy a prompt, like the starters: nothing is sent and no request is made.

def project_prompt(client_id):
    return (
        f"Help me add a project for the client {client_id} to Engagements. "
        "Use the engagements:onboard-client skill. Ask me only what you need."
    )


POPULATED = overview(clients=[client_row("acme-bakery", "Acme Bakery", projects=[
    project_row("acme-website", "Acme Website", {"id": "acme-bakery", "name": "Acme Bakery", "known": True}),
])])
ACME_PROJECT = record_body("acme-website", "Acme Website", "project",
                           client={"id": "acme-bakery", "name": "Acme Bakery", "known": True})


def open_record(frontend, record_id, name, body, *, overview_body=POPULATED):
    return [
        {"op": "render"},
        {"op": "resolve", "path": path(frontend, "/overview"), "body": overview_body},
        select(record_id, name),
        {"op": "resolve", "path": path(frontend, f"/record/{record_id}"), "body": body},
    ]


@pytest.mark.parametrize("frontend", list(FRONT_ENDS))
@pytest.mark.parametrize("which", ["has-a-client", "unassigned-project"])
def test_a_populated_page_shows_add_a_client_under_the_search(frontend, which, tmp_path):
    snapshots = run(frontend, [
        {"op": "render"},
        {"op": "resolve", "path": path(frontend, "/overview"), "body": hiding_body(which)},
    ], tmp_path)
    text = snapshots[-1]["text"]
    assert text.count("Add a client") == 1, text
    assert "Add a project" not in text, "no record is open yet"
    assert snapshots[-1]["inputs"] == [{"placeholder": "Find a client or project", "value": ""}]


@pytest.mark.parametrize("frontend", list(FRONT_ENDS))
def test_add_a_client_has_a_row_of_its_own_and_the_prompt_buttons_never_wrap(frontend, tmp_path):
    """The list column is too narrow for the search field and a button side by side. In a real
    browser, a button beside the field cut the placeholder short and wrapped its own label over
    two or three lines. So the field keeps its full-width row, the button sits on the next one,
    and both prompt buttons carry an inline no-wrap style (a class may not be in the host's CSS)."""
    snapshots = run(frontend, open_record(frontend, "acme-bakery", "Acme Bakery",
                                          record_body("acme-bakery", "Acme Bakery")), tmp_path)
    buttons = {b["text"]: b for b in snapshots[-1]["buttons"] if b["text"] in ("Add a client", "Add a project")}
    assert set(buttons) == {"Add a client", "Add a project"}, snapshots[-1]["buttons"]
    assert buttons["Add a client"]["besideInput"] is False, "the search field keeps a row to itself"
    assert buttons["Add a client"]["nowrap"], "the label must stay on one line"
    assert buttons["Add a project"]["nowrap"], "the label must stay on one line"


def test_desktop_keeps_a_client_records_two_actions_together(tmp_path):
    """Loose in the header row, the pair split in a narrower Desktop window: "Add a project"
    stayed at the right of the first line and "Start a chat" dropped to the left of the next.
    In a container of their own they wrap as one unit."""
    snapshots = run("desktop", open_record("desktop", "acme-bakery", "Acme Bakery",
                                           record_body("acme-bakery", "Acme Bakery")), tmp_path)
    buttons = {b["text"]: b for b in snapshots[-1]["buttons"]}
    assert buttons["Add a project"]["group"] == ["Add a project", "Start a chat"], buttons["Add a project"]
    assert buttons["Start a chat"]["group"] == ["Add a project", "Start a chat"], buttons["Start a chat"]
    assert buttons["Start a chat"]["nowrap"], "the label must stay on one line"


def test_desktop_add_a_client_on_a_populated_page_drafts_the_starter_prompt(tmp_path):
    snapshots = run("desktop", [
        {"op": "render"},
        {"op": "resolve", "path": "/overview", "body": POPULATED},
        {"op": "click", "text": "Add a client"},
    ], tmp_path, draft_works=True)
    calls = snapshots[-1]["calls"]
    assert calls["newChat"] == [True]
    assert calls["setDraft"] == [{"target": "new", "draft": ADD_PROMPT}]
    assert calls["composerSubmit"] == 0, "the user presses Enter; the button never sends"
    assert snapshots[-1]["notifications"][-1]["message"] == "New chat ready. Press Enter to send the prompt."


def test_the_web_tab_add_a_client_on_a_populated_page_copies_the_starter_prompt(tmp_path):
    snapshots = run("web", [
        {"op": "render"},
        {"op": "resolve", "path": path("web", "/overview"), "body": POPULATED},
        {"op": "click", "text": "Add a client"},
    ], tmp_path)
    calls = snapshots[-1]["calls"]
    assert calls["clipboard"] == [ADD_PROMPT]
    text = snapshots[-1]["text"]
    assert ADD_PROMPT in text and "Paste it into a new chat" in text
    assert calls["newChat"] == [] and calls["composerSubmit"] == 0
    assert snapshots[-1]["pending"] == [], "a copy starts no request"


@pytest.mark.parametrize("frontend", list(FRONT_ENDS))
def test_a_client_record_shows_add_a_project(frontend, tmp_path):
    snapshots = run(frontend, open_record(frontend, "acme-bakery", "Acme Bakery",
                                          record_body("acme-bakery", "Acme Bakery")), tmp_path)
    text = snapshots[-1]["text"]
    assert text.count("Add a project") == 1, text


@pytest.mark.parametrize("frontend", list(FRONT_ENDS))
def test_a_project_record_does_not_show_add_a_project(frontend, tmp_path):
    snapshots = run(frontend, open_record(frontend, "acme-website", "Acme Website", ACME_PROJECT), tmp_path)
    text = snapshots[-1]["text"]
    assert "Acme Website" in text and "project" in text
    assert "Add a project" not in text


@pytest.mark.parametrize("frontend", list(FRONT_ENDS))
def test_a_record_the_page_cannot_list_does_not_show_add_a_project(frontend, tmp_path):
    """A selection the latest list no longer offers shows its reason and no actions at all."""
    snapshots = run(frontend, [
        *open_record(frontend, "acme-bakery", "Acme Bakery", record_body("acme-bakery", "Acme Bakery")),
        {"op": "click", "text": "Refresh"},
        {"op": "resolve", "path": path(frontend, "/overview"),
         "body": overview(clients=[client_row("riverbend-dental", "Riverbend Dental")])},
    ], tmp_path)
    assert "Add a project" in snapshots[3]["text"], "the record offered it while it was listed"
    text = snapshots[-1]["text"]
    assert "This record is not listed" in text
    assert "Add a project" not in text


def test_desktop_add_a_project_drafts_the_prompt_with_the_client_id(tmp_path):
    snapshots = run("desktop", [
        *open_record("desktop", "acme-bakery", "Acme Bakery", record_body("acme-bakery", "Acme Bakery")),
        {"op": "click", "text": "Add a project"},
    ], tmp_path, draft_works=True)
    calls = snapshots[-1]["calls"]
    assert calls["newChat"] == [True]
    assert calls["setDraft"] == [{"target": "new", "draft": project_prompt("acme-bakery")}]
    assert calls["composerSubmit"] == 0, "the user presses Enter; the button never sends"
    assert snapshots[-1]["pending"] == [], "a draft starts no request"


def test_desktop_add_a_project_drafts_the_id_under_privacy_like_start_a_chat(tmp_path):
    """Privacy mode changes what is on screen, not what a draft carries: "Start a chat" drafts
    `/client <id>` either way, and the notice names no record."""
    snapshots = run("desktop", [
        *open_record("desktop", "acme-bakery", "Acme Bakery", record_body("acme-bakery", "Acme Bakery")),
        {"op": "togglePrivacy"},
        {"op": "click", "text": "Add a project"},
    ], tmp_path, draft_works=True)
    calls = snapshots[-1]["calls"]
    assert calls["setDraft"] == [{"target": "new", "draft": project_prompt("acme-bakery")}]
    assert "acme-bakery" not in snapshots[-1]["text"]
    assert "acme-bakery" not in snapshots[-1]["notifications"][-1]["message"]


def test_the_web_tab_add_a_project_copies_the_prompt_with_the_client_id(tmp_path):
    snapshots = run("web", [
        *open_record("web", "acme-bakery", "Acme Bakery", record_body("acme-bakery", "Acme Bakery")),
        {"op": "click", "text": "Add a project"},
    ], tmp_path)
    calls = snapshots[-1]["calls"]
    assert calls["clipboard"] == [project_prompt("acme-bakery")]
    text = snapshots[-1]["text"]
    assert project_prompt("acme-bakery") in text and "Paste it into a new chat" in text
    assert calls["newChat"] == [] and calls["composerSubmit"] == 0
    assert snapshots[-1]["pending"] == [], "a copy starts no request"


def test_the_web_tab_add_a_project_hides_the_id_on_screen_under_privacy(tmp_path):
    """The shown prompt hides the id as the "Start a chat" row does (`/client …`); the
    clipboard holds the full prompt, as the row's command would."""
    snapshots = run("web", [
        *open_record("web", "acme-bakery", "Acme Bakery", record_body("acme-bakery", "Acme Bakery")),
        {"op": "togglePrivacy"},
        {"op": "click", "text": "Add a project"},
    ], tmp_path)
    assert snapshots[-1]["calls"]["clipboard"] == [project_prompt("acme-bakery")]
    text = snapshots[-1]["text"]
    assert project_prompt("…") in text
    assert "acme-bakery" not in text


@pytest.mark.parametrize("frontend", list(FRONT_ENDS))
@pytest.mark.parametrize("state", ["missing", "unavailable", "broken-library-yaml", "failed"])
def test_neither_add_button_shows_when_the_overview_is_not_ok(frontend, state, tmp_path):
    """A button must not promise work that cannot be saved. A missing library keeps its starter
    block, whose own "Add a client" is the only one on the page."""
    if state == "failed":
        step = {"op": "reject", "path": path(frontend, "/overview"), "message": "The Engagements backend is not running."}
    elif state == "missing":
        step = {"op": "resolve", "path": path(frontend, "/overview"), "body": overview(status="missing", message=MISSING)}
    else:
        message = UNAVAILABLE_TEXT if state == "unavailable" else "library.yaml could not be read."
        step = {"op": "resolve", "path": path(frontend, "/overview"), "body": overview(status="unavailable", message=message)}
    snapshots = run(frontend, [{"op": "render"}, step], tmp_path)
    text = snapshots[-1]["text"]
    assert "Add a project" not in text
    assert snapshots[-1]["inputs"] == [], "no search field, so no Add a client beside one"
    if state == "missing":
        assert text.count("Add a client") == 1 and "Find clients in my past chats" in text
    else:
        assert "Add a client" not in text


# --- the Node switch itself -------------------------------------------------------------------


def test_a_present_node_is_returned_even_when_the_environment_demands_it():
    assert require_node("/usr/bin/node", demanded=True) == "/usr/bin/node"


def test_a_missing_node_is_left_to_the_skip_marker_when_it_is_not_demanded():
    assert require_node(None, demanded=False) is None


def _pytest_without_node(tmp_path, *, demanded, collect_only=False):
    """Run this file in a subprocess whose PATH holds no Node.

    The two switch tests that start a subprocess of their own are deselected, because this
    run *is* the inner pytest they would start.
    """
    module = ROOT / "tests" / "test_dashboard_frontends.py"
    env = {key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", NODE_ENV}}
    env["PATH"] = str(tmp_path)
    if demanded:
        env[NODE_ENV] = "1"
    arguments = [sys.executable, "-m", "pytest", "-q", "-rs", "-p", "no:cacheprovider"]
    if collect_only:
        arguments.append("--collect-only")
    for name in (
        "test_a_demanded_missing_node_stops_collection_instead_of_skipping",
        "test_a_missing_node_without_the_switch_still_skips_on_an_ordinary_checkout",
    ):
        arguments += ["--deselect", f"tests/test_dashboard_frontends.py::{name}"]
    arguments.append(str(module))
    return subprocess.run(arguments, cwd=ROOT, env=env, capture_output=True, text=True, timeout=180, check=False)


def test_a_demanded_missing_node_stops_collection_instead_of_skipping(tmp_path):
    """The workflow installs Node and demands it, so a provisioning failure cannot pass quietly."""
    result = _pytest_without_node(tmp_path, demanded=True, collect_only=True)
    assert result.returncode != 0, result.stdout
    assert "node is required" in result.stdout + result.stderr


def test_a_missing_node_without_the_switch_still_skips_on_an_ordinary_checkout(tmp_path):
    """Without the switch the ordinary checkout runs this file and each test skips, not passes."""
    result = _pytest_without_node(tmp_path, demanded=False)
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "node is not installed" in output, f"the tests did not report why they skipped:\n{output}"
    assert "skipped" in output, f"no test skipped, so the marker no longer guards them:\n{output}"
