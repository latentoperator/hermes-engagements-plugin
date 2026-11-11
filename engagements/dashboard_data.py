"""Read-only views of the library for the Desktop page and the web dashboard tab (SPEC 6.7).

Every function here takes a ``runtime.Host`` and returns plain JSON-ready data. Nothing
writes: the library is opened with ``library.open_library_readonly`` (no lock, no
directory, no metadata), records are read without a snapshot (``tools._read_plain``),
doctor is the read-only doctor, and the binding is resolved against the library this
module already opened (``tools.binding_for_library``), never through a second, creating
open. The ``Host`` the dashboard backend builds also refuses ``state_set`` so a future
change cannot write by accident.

Like the rest of ``engagements/``, this module does not import Hermes.
"""

import os
from datetime import datetime

from . import binding, doctor, records
from .errors import EngagementsError
from .library import open_library_readonly
from .runtime import missing_text, resolve_config
from .tools import (
    Session,
    _build_view,
    _locations_from,
    _read_plain,
    binding_for_library,
)

# A record body larger than this is shown truncated; the agent and the editor are the write paths.
BODY_LIMIT = 200_000
_STATUS_ORDER = {"active": 0, None: 1}

# Two records under one id cannot be told apart by an id-addressed page: the tools refuse such an
# id (``library.resolve`` raises ambiguous) and so does the dashboard (D11: it lists records, one
# per id). The notice points at the doctor, which already reports every file that shares an id.
_DUPLICATE_MESSAGE = (
    "The id '{record_id}' is used by more than one record, so this page cannot tell them apart. "
    "Give each record its own id, then try again. hermes engagements doctor lists the files that share one."
)

# One plain sentence per record the page cannot list. The reason names the defect that keeps the
# record off the list and is written for the user, not the tools: no ids, names or paths, so the
# notice stays safe to render with privacy on.
_DUPLICATE_REASON = "Its id is used by more than one record."
_NO_ID_REASON = "Its id could not be read."
_NO_KIND_REASON = "Its kind could not be read as a client or project."


def _sharable(entry):
    """A record the page can list and open: a client or project whose id is its own."""
    return entry.id is not None and entry.kind in ("client", "project") and "duplicate_id" not in entry.defects


def _unlisted_reason(entry):
    """Why the notice cannot list this record.

    A shared id is named first, whatever else is wrong with the record: a record with a
    duplicated id and an unreadable kind is still a duplicate, and hiding that behind another
    defect would understate what the user has to fix.
    """
    if "duplicate_id" in entry.defects:
        return _DUPLICATE_REASON
    if entry.id is None:
        return _NO_ID_REASON
    return _NO_KIND_REASON


class ReadOnlyState(Exception):
    """Raised if anything on the dashboard path tries to write plugin state."""


def refuse_state_set(key, value):  # noqa: ARG001 - the signature is ctx.state.set's
    raise ReadOnlyState("The dashboard is read-only.")


def _session(host, config, session_id=None):
    return Session(session_id=session_id, cwd=None, surface=None, profile=None, config=config,
                   state_get=host.state_get, state_set=refuse_state_set, clock=host.clock)


def _abs(root, relpath):
    return os.path.join(os.fspath(root), *relpath.split("/"))


def _location(raw):
    """A project location for display: the stored text, the expanded path, whether it is a local
    folder form at all, and whether that folder exists here.

    Only local absolute paths are folders (SPEC 4); a URL, UNC or relative entry is a reference,
    with ``local`` false, so the views never call it a missing folder.
    """
    expanded = os.path.expanduser(raw)
    local = binding.is_local_path(expanded, os.name)
    try:
        found = local and os.path.isdir(expanded)
    except (OSError, ValueError):
        found = False
    return {"path": raw, "expanded": expanded, "local": local, "found": found}


def _status_key(item):
    return (_STATUS_ORDER.get(item.get("status"), 2), (item.get("name") or "").casefold(), item.get("id") or "")


def _library_state(config):
    """``(status, message)`` before opening: why there is nothing to show, in the user's words."""
    if config.library_root is None:
        return "unavailable", missing_text(config)
    if not os.path.isdir(os.fspath(config.library_root)):
        return "missing", missing_text(config)
    return "ok", None


def _open(config):
    """``(library or None, status, message)``. Never creates or locks anything (SPEC 6.7).

    ``open_library_readonly`` deliberately skips the writing opener's re-read under the
    library lock, so an invalid ``library.yaml`` is reported as unavailable instead of
    creating ``.engagements/locks/_library.lock``.
    """
    status, message = _library_state(config)
    if status != "ok":
        return None, status, message
    try:
        return open_library_readonly(config.library_root), "ok", None
    except EngagementsError as exc:
        return None, "unavailable", exc.message


def _base(config, status, message):
    return {
        "status": status,
        "message": message,
        "library_root": None if config.library_root is None else os.fspath(config.library_root),
        "labels": dict(config.labels),
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def overview(host):
    """Every client with its projects, project folders found or missing, and the doctor summary."""
    config = resolve_config(host)
    lib, status, message = _open(config)
    data = _base(config, status, message)
    data.update(clients=[], unassigned=[], unreadable=[], duplicated=[], unlisted=[],
                counts={"clients": 0, "projects": 0, "active_projects": 0}, doctor=None)
    if lib is None:
        return data
    discovery = lib.discover()
    clients, projects = {}, []
    for entry in discovery.entries:
        if "duplicate_id" in entry.defects or entry.id is None or entry.kind not in ("client", "project"):
            # Not a row: the list shows one record per id and the detail view opens one by id, so
            # two records under one id would list one and open the other. Every entry that is not
            # a row is named exactly once in the notice with its path and a plain reason, so the
            # page never leaves a record out silently. A shared id is decided first, so no other
            # defect can keep a duplicate out of the notice.
            notice = {"id": entry.id, "path": entry.relpath, "reason": _unlisted_reason(entry),
                      "defects": sorted(entry.defects)}
            data["unlisted"].append(notice)
            if "duplicate_id" in entry.defects:
                data["duplicated"].append(dict(notice, kind=entry.kind))
            else:
                data["unreadable"].append(notice)
            continue
        row = {"id": entry.id, "kind": entry.kind, "name": entry.name or entry.id, "status": entry.status,
               "valid": entry.valid, "path": entry.relpath, "abs_path": _abs(lib.root, entry.relpath),
               "folder": os.path.dirname(_abs(lib.root, entry.relpath))}
        if entry.kind == "client":
            row["projects"] = []
            clients[entry.id] = row
        else:
            row["client"] = entry.client
            row["locations"] = [_location(p) for p in _project_locations(lib, entry)]
            projects.append(row)
    for project in projects:
        owner = clients.get(project["client"])
        (owner["projects"] if owner is not None else data["unassigned"]).append(project)
    for client in clients.values():
        client["projects"].sort(key=_status_key)
    data["clients"] = sorted(clients.values(), key=_status_key)
    data["unassigned"].sort(key=_status_key)
    data["duplicated"].sort(key=lambda item: item["path"])
    data["unreadable"].sort(key=lambda item: item["path"])
    data["unlisted"].sort(key=lambda item: item["path"])
    data["counts"] = {"clients": len(clients), "projects": len(projects),
                      "active_projects": sum(1 for p in projects if p["status"] == "active")}
    report = doctor.run_doctor(lib.root)
    data["doctor"] = {"clean": report.clean, "counts": report.counts(), "checked": report.checked}
    return data


def _project_locations(lib, entry):
    try:
        parsed = records.parse(_read_plain(lib, entry.relpath))
    except Exception:  # noqa: BLE001 - an unreadable project simply shows no folders
        return []
    if (parsed.id, parsed.kind) != (entry.id, entry.kind):
        return []
    return _locations_from(parsed)


def record(host, record_id):
    """One record's properties and sections, read without a snapshot.

    ``None`` when there is no such id; a ``conflict`` envelope when the id is used by more than
    one record, because neither of them can be answered for it.
    """
    config = resolve_config(host)
    lib, status, message = _open(config)
    data = _base(config, status, message)
    if lib is None:
        return data
    discovery = lib.discover()
    # Ambiguity is decided over every discovery entry carrying the id, before the kind filter:
    # three files with an unreadable kind still share the id, so the id is ambiguous rather than
    # absent, and no body is read for it. A single entry that is not a client or project is not
    # something this view can answer either, so it stays absent.
    carrying = [e for e in discovery.entries if e.id == record_id]
    if not carrying:
        return None
    if len(carrying) > 1 or any("duplicate_id" in e.defects for e in carrying):
        # The same rule the list applies: never answer with one of two records under this id.
        data.update(status="conflict", message=_DUPLICATE_MESSAGE.format(record_id=record_id))
        return data
    matches = [e for e in carrying if e.kind in ("client", "project")]
    if not matches:
        return None
    entry = matches[0]
    raw = _read_plain(lib, entry.relpath)
    parsed = records.parse(raw)
    if (parsed.id, parsed.kind) != (entry.id, entry.kind):
        return None  # the file changed between discovery and the read: never show another record under this id
    truncated = False
    if "encoding" in parsed.global_defects:
        text = raw.decode("utf-8", errors="replace")
        truncated = len(text) > BODY_LIMIT
        sections = [{"title": "", "text": text[:BODY_LIMIT]}]
    else:
        view, placed = _build_view(raw, parsed, None)
        sections, budget = [], BODY_LIMIT
        for section, _start, body, stop in placed:
            text = view[body:stop]
            if len(text) > budget:
                text, truncated = text[:budget], True
            sections.append({"title": section.title, "text": text})
            budget -= len(text)
            if budget <= 0:
                truncated = truncated or len(sections) < len(placed)
                break
    properties = dict(parsed.properties or {})
    if entry.kind == "project":
        properties["locations"] = [_location(p) for p in _locations_from(parsed)]
    abs_path = _abs(lib.root, entry.relpath)
    # ``status`` is the envelope's (a healthy record is "ok"); the record's own status is
    # ``record_status``. Overwriting ``status`` here made a healthy record indistinguishable
    # from an unavailable library for any reader that checks the envelope first.
    data.update(id=entry.id, kind=entry.kind, name=parsed.name or entry.id, record_status=entry.status, valid=entry.valid,
                defects=sorted(entry.defects), path=entry.relpath, abs_path=abs_path,
                folder=os.path.dirname(abs_path), properties=properties, sections=sections, truncated=truncated)
    if entry.kind == "client":
        # Only projects the list would show under this client: one carrying a duplicated id is
        # not a row either, so it cannot appear here as one (the list and this view must agree).
        data["projects"] = sorted(
            ({"id": e.id, "name": e.name or e.id, "status": e.status} for e in discovery.entries
             if _sharable(e) and e.kind == "project" and e.client == entry.id), key=_status_key)
    else:
        # A client id two records share names no single owner, the same way the list leaves such a
        # project unassigned, so the viewer is never shown one duplicate's name for the other's id.
        owner = next((e for e in discovery.entries if _sharable(e) and e.kind == "client" and e.id == entry.client), None)
        # ``known`` is what a viewer needs to decide whether a chat can be started for this
        # project: an unassigned project has no client record to bind to.
        data["client"] = {"id": entry.client, "name": None if owner is None else owner.name,
                          "known": owner is not None}
    return data


def session_view(host, session_id):
    """Which client a session is bound to, as stored. Read-only: nothing is resolved from a folder here."""
    config = resolve_config(host)
    lib, status, message = _open(config)
    data = _base(config, status, message)
    data.update(session=bool(session_id), mode=None, state=None, client=None, project=None, automatic=False)
    if lib is None or not session_id:
        return data
    # The binding is resolved against the library this view already opened, so a library
    # that vanishes between the open and here is never recreated with a new identity by a
    # second, creating open.
    state, stored = binding_for_library(lib, _session(host, config, session_id))
    data["mode"] = state.mode
    data["state"] = state.state
    if state.state == "bound":
        data["client"] = {"id": state.client_id, "name": state.client_name}
        if state.project_id:
            data["project"] = {"id": state.project_id, "name": state.project_name}
        return data
    if state.state == "cwd_unsupported" and isinstance(stored, dict):
        # An automatic binding the agent made from the folder on its last turn. The agent revalidates
        # it every turn; the dashboard has no folder of its own, so it reports what is stored.
        # An id two records share names no single record, so it is left out rather than letting
        # the last one discovered answer for the others (the chip then shows no client).
        names = {e.id: e.name for e in lib.discover().entries if _sharable(e)}
        client, project = stored.get("client"), stored.get("project")
        if isinstance(client, str) and client in names:
            data["client"] = {"id": client, "name": names[client]}
            data["automatic"] = True
            if isinstance(project, str) and project in names:
                data["project"] = {"id": project, "name": names[project]}
    return data


def doctor_view(host):
    """The read-only doctor report as data."""
    config = resolve_config(host)
    status, message = _library_state(config)
    data = _base(config, status, message)
    if status != "ok":
        data["report"] = None
        return data
    data["report"] = doctor.run_doctor(config.library_root).as_dict()
    return data
