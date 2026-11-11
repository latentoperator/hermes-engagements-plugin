"""The five Engagements tool handlers (docs/TOOLS.md), as pure functions of ``(args, session)``.

Each handler validates the model-facing arguments (stage 1), opens the library
(stage 2), resolves its target (stage 3) and then delegates to the library,
writer and binding modules, which own stages 4 to 8. Every handler returns the
common envelope and never raises: an ``EngagementsError`` becomes ``error`` and
any other exception becomes ``io_error`` without a traceback.

Time, the session and the state store are injected through ``Session``; nothing
here reads the system clock or keeps process-global state. Nothing under
``engagements/`` imports Hermes (AGENTS.md).
"""

import copy
import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from . import binding, records, writer
from . import library as library_module
from .errors import EngagementsError
from .fsutil import normalize_text, section_key
from .library import open_library
from .redact import (
    location_text,
    new_findings,
    redact_label,
    redact_text,
    secret_findings,
)
from .schema import MISSING, UNEXPECTED, validate
from .tool_schemas import DESCRIPTIONS, OP_SCHEMAS, PARAMETERS

PAGE_SIZE = 12000
SECTION_TEXT_CAP = 4000
# The bounds on one ``engagements_context`` result. The response as a whole is
# bounded (``CONTEXT_RESPONSE_BYTES``); these clip its parts first, so the body page
# is the only thing the last guard has to give up. Each constant names what it
# bounds: a heading in the section index, the section index of one page, a property
# value, a list value's items, a property key and the property map, a related row's
# name and locations, the related rows, and the diagnostics.
SECTION_TITLE_CAP = 200
CONTEXT_SECTIONS_CAP = 400
PROPERTY_VALUE_CAP = 2000
PROPERTY_LIST_CAP = 32
PROPERTY_KEY_CAP = 120
CONTEXT_PROPERTY_KEYS_CAP = 64
CONTEXT_PROPERTIES_BYTES = 12_000
PROPERTY_SMALL_BYTES = 200
RELATED_NAME_CAP = 200
RELATED_LOCATION_CAP = 200
RELATED_LOCATIONS_CAP = 20
CONTEXT_PROJECTS_CAP = 50
CONTEXT_RELATED_BYTES = 8_000
CONTEXT_WARNINGS_CAP = 16
WARNING_LABEL_CAP = 120
# The whole envelope -- ``{"ok", "data", "warnings"}`` serialized as JSON, which is
# what the transport sends -- stays within this many bytes for any record inside the
# record limit. ``CONTEXT_RESPONSE_MARGIN`` leaves room for what a handler does not
# build itself (the ok flag, separators) and ``CONTEXT_PAGE_BYTES`` keeps a page
# big enough to make progress.
CONTEXT_RESPONSE_BYTES = 64 * 1024
CONTEXT_RESPONSE_MARGIN = 1_024
CONTEXT_PAGE_BYTES = 1_024
CONTEXT_SHRINK_STEP = 512
CONTEXT_SHRINK_SNAP = 200
BIND_SECTIONS = ("summary", "contacts", "locations")
UNREADABLE_CAP = 10  # library-relative paths one response lists; evidence carries the total
TOOL_NAMES = ("engagements_find", "engagements_bind", "engagements_context", "engagements_create",
              "engagements_update")

TOOL_SCHEMAS = {name: {"name": name, "description": DESCRIPTIONS[name], "parameters": PARAMETERS[name]}
                for name in TOOL_NAMES}

# A write is never refused because it looks like it holds a credential: it is saved
# and the result carries one warning per finding. The message is written once here,
# is addressed to the agent, and names only the structural location: no value, no
# property name, no heading and no snippet from the record can reach it. See
# docs/TOOLS.md "Common result envelope".
POSSIBLE_SECRET_CODE = "possible_secret"
POSSIBLE_SECRET_MESSAGE = (
    "The record was saved. It appears to contain a credential at {location}. "
    "Stop and tell the user now, before any other action. Remove the value from the record. "
    "Treat the credential as exposed and rotate it if it was real. Records are shared files "
    "and must never hold secrets."
)
POSSIBLE_SECRET_OMITTED = (
    "More locations in this record look like credentials than are listed here. "
    "Stop and tell the user now, before any other action, and run doctor for the rest."
)
SECRET_WARNINGS_PER_WRITE = 5

_REASON_TEXT = {
    "record_inactive": "it is inactive",
    "record_malformed": "its record is malformed",
    "record_missing": "its record is missing",
    "record_identity_changed": "it is no longer the same record",
    "duplicate_id": "its id is duplicated",
    "kind_mismatch": "it is not the expected kind of record",
}
_ABSOLUTE_WINDOWS = re.compile(r"(?<![\w./\\:~-])(?:\\\\\?\\|[A-Za-z]:[\\/]|\\\\)[^\s'\"<>,;)\]}]*")
_ABSOLUTE = re.compile(r"(?<![\w./:~-])/(?:[^\s'\"<>,;)\]}]+)")


# --- the session ---------------------------------------------------------------------


@dataclass
class SessionConfig:
    """The plugin's settings, already resolved by the caller (spec 6.1)."""

    library_root: Any = None
    labels: dict = field(default_factory=lambda: {"client": "Client", "project": "Project"})
    auto_bind: bool = True
    banner: bool = True
    create_default: bool = False  # True only when library_root is the default location


@dataclass
class Session:
    """Everything a handler may know about the conversation (TOOLS.md "Test hooks")."""

    session_id: str | None
    cwd: str | None
    surface: str | None
    profile: str | None
    config: Any
    state_get: Callable[[str], Any]
    state_set: Callable[[str, Any], None]
    clock: Callable[[], datetime]


def _config(session):
    config = session.config
    if isinstance(config, SessionConfig):
        return config
    if isinstance(config, dict):
        known = {k: v for k, v in config.items() if k in SessionConfig.__dataclass_fields__}
        return SessionConfig(**known)
    return SessionConfig(library_root=getattr(config, "library_root", None),
                         auto_bind=getattr(config, "auto_bind", True),
                         create_default=getattr(config, "create_default", False))


def _profile(session):
    profile = session.profile
    return profile if records.is_single_line(profile, 200) else "default"


# --- envelopes and scrubbing ------------------------------------------------------------


def _scrub_text(text, roots):
    """Show a path inside the library root as relative, any other absolute path by name only."""

    def replace(match):
        raw = match.group(0)
        for root in roots:
            if raw == root:
                return "."
            if raw.startswith(root.rstrip("/") + "/"):
                return raw[len(root.rstrip("/")) + 1:]
        return raw.rstrip("/").rsplit("/", 1)[-1] or "/"

    def replace_windows(match):
        flat = match.group(0).replace("\\", "/")
        flat = flat[4:] if flat.startswith("//?/") else flat
        for root in roots:
            base = root.replace("\\", "/")
            base = (base[4:] if base.startswith("//?/") else base).rstrip("/")
            if not re.match(r"[A-Za-z]:|//", base):
                continue
            if flat.rstrip("/").lower() == base.lower():
                return "."
            if flat.lower().startswith(base.lower() + "/"):
                return flat[len(base) + 1:]
        return flat.rstrip("/").rsplit("/", 1)[-1] or "/"

    return _ABSOLUTE.sub(replace, _ABSOLUTE_WINDOWS.sub(replace_windows, text))


_CONTRACT_FOOTPRINT_FIELDS = frozenset({"base_text", "current_text"})
# Diagnostic fields whose value is one label (an argument name, a key, a heading,
# an id), not prose. A flagged label is replaced whole, never span by span, so no
# part of it survives. Prose fields (``message``,
# ``evidence``) keep their readable parts and lose only the secret spans.
_LABEL_FIELDS = frozenset({"field", "key", "section", "heading", "title", "id", "name", "record", "client",
                           "project", "project_client", "requested_client"})


def _sanitize_text(text, roots):
    """A diagnostic string: paths relative, secret-shaped spans replaced."""
    return redact_text(_scrub_text(text, roots))


def _sanitize_key(key, roots):
    """A diagnostic dict key is a label: scrubbed, and replaced whole if secret-shaped."""
    return redact_label(_scrub_text(key, roots)) if isinstance(key, str) else key


def _sanitize_field(name, item, roots):
    """One diagnostic field: a label is replaced whole, anything else is walked."""
    if name in _LABEL_FIELDS and isinstance(item, str):
        return redact_label(_scrub_text(item, roots))
    return _sanitize_diagnostic(item, roots)


def _roots(session):
    roots = []
    try:
        configured = _config(session).library_root
        if configured is not None:
            text = os.path.expanduser(os.fspath(configured))
            roots.extend({text, os.path.realpath(text)})
    except (OSError, TypeError, ValueError):
        pass
    return sorted(roots, key=len, reverse=True)


def _sanitize_diagnostic(value, roots):
    """Sanitize a diagnostic structure at any depth.

    Every string and every dict key is a diagnostic, so a field added later is
    redacted by default instead of passing through.
    """
    if isinstance(value, str):
        return _sanitize_text(value, roots)
    if isinstance(value, dict):
        return {_sanitize_key(key, roots): _sanitize_field(key, item, roots) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize_diagnostic(item, roots) for item in value]
    return value


def _sanitize_footprint(footprint, roots):
    """A conflict footprint: its texts are the contract's body, its labels are not."""
    if not isinstance(footprint, dict):
        return _sanitize_diagnostic(footprint, roots)
    return {_sanitize_key(key, roots):
            (item if key in _CONTRACT_FOOTPRINT_FIELDS else _sanitize_field(key, item, roots))
            for key, item in footprint.items()}


def _sanitize_details(details, roots, *, conflict):
    out = {}
    for key, item in details.items():
        name = _sanitize_key(key, roots)
        if conflict and key == "footprints" and isinstance(item, (list, tuple)):
            # Only a conflict's footprints are contract body (TOOLS.md "Footprints
            # and rebase"); the same shape on any other error is a diagnostic.
            out[name] = [_sanitize_footprint(entry, roots) for entry in item]
        else:
            out[name] = _sanitize_field(key, item, roots)
    return out


def _sanitize_error(error, roots):
    out = {}
    conflict = error.get("code") == "conflict"
    for key, item in error.items():
        name = _sanitize_key(key, roots)
        out[name] = (_sanitize_details(item, roots, conflict=conflict)
                     if key == "details" and isinstance(item, dict)
                     else _sanitize_field(key, item, roots))
    return out


def _sanitize(payload, roots):
    """Sanitize every diagnostic in an envelope; ``data`` is returned verbatim.

    The envelope's shape decides what is a diagnostic: everything under
    ``error`` and ``warnings``, except the conflict footprints' ``base_text``
    and ``current_text``, which TOOLS.md requires as full raw text. Path
    scrubbing and redaction never touch the contract's body, and the fields that
    identify or quote the record (``id``, ``path``, ``name``, section titles,
    ``change_log_line``) are the record's own content, so they are returned as
    written.
    """
    if "warnings" in payload:
        payload["warnings"] = _sanitize_diagnostic(payload.get("warnings"), roots)
    if isinstance(payload.get("error"), dict):
        payload["error"] = _sanitize_error(payload["error"], roots)
    return payload


def _success(data, warnings, roots):
    return _sanitize({"ok": True, "data": data, "warnings": warnings}, roots)


def _failure(code, message, details, warnings, roots):
    error = {"code": code, "message": message, "details": details if isinstance(details, dict) else {}}
    return _sanitize({"ok": False, "error": error, "warnings": warnings}, roots)



def _invalid(field_name, reason, **extra):
    return EngagementsError("invalid_op", "Invalid argument.", {"field": field_name, "reason": reason, **extra})


# Refusals a core module words with the default names. The core layers keep their own
# sentence and their stable codes; the configured labels are known only at the tool boundary,
# so that is where the sentence the model reads is rendered. Keyed by the error's code and
# reason: the value is the sentence the lower layer writes and the sentence rendered here.
LOWER_LAYER_REFUSALS = {
    ("not_found", "no_match"): ("Client not found.", "{client_word} not found."),
}


def _boundary_error(error, labels):
    """A lower layer's refusal, worded from the configured labels at the one boundary.

    A refusal is re-worded only when its code, its reason and its exact default sentence all
    match a declared entry, and when it carries no ``path`` detail. The last two guards are
    what keep an unrelated refusal intact: the writer raises ``not_found``/``no_match`` for a
    record that vanished between discovery and the write, with a path and its own sentence, and
    that refusal is about a file, not about the renamed entity. Everything the table does not
    declare leaves this boundary exactly as the lower layer produced it, codes and details
    included.
    """
    details = error.details if isinstance(error.details, dict) else {}
    reason = details.get("reason")
    entry = LOWER_LAYER_REFUSALS.get((error.code, reason)) if isinstance(reason, str) else None
    if entry is None or "path" in details or error.message != entry[0]:
        return error
    words = binding.declared_labels(labels)
    return EngagementsError(error.code, entry[1].format(client_word=words["client"], project_word=words["project"]),
                            dict(details))


def _run(impl, args, session):
    warnings = []
    roots = _roots(session)
    try:
        return _success(impl(args, session, warnings), warnings, roots)
    except EngagementsError as exc:
        exc = _boundary_error(exc, _config(session).labels)
        return _failure(exc.code, exc.message, exc.details, warnings, roots)
    except Exception:  # noqa: BLE001 - the contract: no traceback text ever leaves a handler
        return _failure("io_error", "Filesystem operation failed.", {}, warnings, roots)


def _add_warnings(sink, items):
    for item in items or ():
        if item.get("code") == "library_created" and any(w.get("code") == "library_created" for w in sink):
            continue
        sink.append(item)


# --- possible secrets in a write -------------------------------------------------


def _bounded(found):
    """At most :data:`SECRET_WARNINGS_PER_WRITE` warnings, plus one count of the rest."""
    if not found:
        return []
    bounded = found[:SECRET_WARNINGS_PER_WRITE]
    if len(found) > len(bounded):
        bounded.append({"code": POSSIBLE_SECRET_CODE, "message": POSSIBLE_SECRET_OMITTED,
                        "omitted": len(found) - len(bounded)})
    return bounded


def _secret_warnings(found):
    """``possible_secret`` warnings built from structural findings only.

    Each finding is ``{"line", "area", "section", "kind", "occurrence"}`` from
    :func:`engagements.redact.secret_findings`, so the warning names where the
    value sits and never what it is. At most
    :data:`SECRET_WARNINGS_PER_WRITE` locations are listed; the rest are counted in
    one further entry.
    """
    entries = []
    for finding in found:
        entry = {"code": POSSIBLE_SECRET_CODE,
                 "message": POSSIBLE_SECRET_MESSAGE.format(
                     location=location_text(finding["area"], finding["section"], finding["line"],
                                            finding["occurrence"])),
                 "area": finding["area"], "line": finding["line"], "kind": finding["kind"],
                 "occurrence": finding["occurrence"]}
        if finding["section"] is not None:
            entry["section"] = finding["section"]
        entries.append(entry)
    return _bounded(entries)


def _record_text(lib, relpath):
    """A record's text, or ``""`` when it cannot be read.

    A read failure here is never a tool result: the write path already reported
    what it wrote. ``""`` for the record before an update can only add warnings
    (every value looks new), and ``""`` for the record after it adds none.
    """
    try:
        return (Path(lib.root) / relpath).read_bytes().decode("utf-8", "replace")
    except OSError:
        return ""


# --- stage 1: argument validation ---------------------------------------------------------


def _first_failure(args, schema):
    failures = validate(args, schema)
    ordered = [f for f in failures if f.reason != UNEXPECTED] + [f for f in failures if f.reason == UNEXPECTED]
    return ordered[0] if ordered else None


def _require_object(args):
    if not isinstance(args, dict):
        raise _invalid("arguments", "invalid_value")


def _check_schema(name, args):
    failure = _first_failure(args, PARAMETERS[name])
    if failure is not None:
        raise _invalid(failure.path, failure.reason)


REFERENCE_LIMIT = 200


def _check_reference(name, value):
    """Stage 1 for a lookup reference (an id, name or alias).

    A reference is matched against record text, so it is held to the same single-line rule as
    every other value. Checking it here rather than inside resolution is what makes a rejected
    call reject the same way with and without a library, and keeps a rejected call from
    creating the default library on the way to refusing it.
    """
    if value is not None and not records.is_single_line(value, REFERENCE_LIMIT):
        raise _invalid(name, "invalid_value")


def _is_change_log(value):
    return isinstance(value, str) and section_key(value) == "change log"


def _validate_find(args):
    _require_object(args)
    _check_schema("engagements_find", args)


def _validate_bind(args):
    _require_object(args)
    _check_schema("engagements_bind", args)
    _check_reference("client", args.get("client"))
    _check_reference("project", args.get("project"))
    given = [name for name in ("client", "project") if args.get(name) is not None]
    mode = args.get("mode") or ("explicit" if given else "off")
    if mode == "explicit" and not given:
        raise _invalid("mode", "explicit_requires_target")
    if mode != "explicit" and given:
        raise _invalid(given[0], "mode_forbids_target")
    return mode


def _validate_context(args):
    _require_object(args)
    _check_schema("engagements_context", args)
    _check_reference("id", args.get("id"))
    if "id" in args and "path" in args:
        raise _invalid("path", "id_and_path")
    if args.get("offset", 0) > 0 and "rev" not in args:
        raise _invalid("rev", "rev_required")


def _validate_create(args):
    _require_object(args)
    # Reserved names are recognized before generic schema rejection (TOOLS.md precedence).
    properties = args.get("properties")
    if isinstance(properties, dict) and "updated" in properties:
        raise _invalid("properties.updated", "plugin_owned")
    sections = args.get("sections")
    if isinstance(sections, list):
        for index, item in enumerate(sections):
            if isinstance(item, dict) and _is_change_log(item.get("section")):
                raise _invalid(f"sections[{index}].section", "plugin_owned")
    _check_schema("engagements_create", args)
    library_module._validate_create(args["kind"], args["name"], args.get("client"), properties, sections,
                                    args.get("source", "conversation"), args.get("allow_similar", False))


def _update_schema():
    schema = copy.deepcopy(PARAMETERS["engagements_update"])
    schema["properties"]["ops"]["items"] = {}
    return schema


def _validate_update(args):
    _require_object(args)
    ops = args.get("ops")
    if isinstance(ops, list):
        for index, op in enumerate(ops):
            if not isinstance(op, dict):
                continue
            if op.get("key") == "updated":
                raise EngagementsError("invalid_op", "Invalid operation.",
                                       {"op_index": index, "field": "key", "reason": "plugin_owned"})
            if _is_change_log(op.get("section")):
                raise EngagementsError("invalid_op", "Invalid operation.",
                                       {"op_index": index, "field": "section", "reason": "plugin_owned"})
    failures = validate(args, _update_schema())
    leading = [f for f in failures if f.reason != UNEXPECTED]
    # Single-line checks are stage 1 and follow the schema's property order
    # (TOOLS.md line 67). ``source`` is the last scalar before ``ops``, so a
    # multiline source is invalid_op before the library opens and before the id
    # is resolved: it can never surface as not_found (contract decision).
    source = args.get("source")
    first_field = leading[0].path.split(".", 1)[0] if leading else None
    if (isinstance(source, str) and not records.is_single_line(source, 200)
            and first_field not in ("id", "base_rev")):
        raise _invalid("source", "invalid_value")
    if leading:
        raise _invalid(leading[0].path, leading[0].reason)
    records.validate_shapes(ops)  # per-op schemas, with op_index
    if failures:
        raise _invalid(failures[0].path, failures[0].reason)


# --- stage 2 and shared lookups ------------------------------------------------------------


def _open(session, warnings):
    root = _config(session).library_root
    if root is None:
        raise EngagementsError("library_missing", "No library root is configured.", {"reason": "no_library_root"})
    lib = open_library(root, create_default=_config(session).create_default)
    _add_warnings(warnings, lib.base_warnings())
    return lib


def _read_plain(lib, relpath):
    """The record's bytes without taking a snapshot."""
    with writer._opened_root(lib) as (root_path, top):
        parts, name, relative = writer._resolve(root_path, relpath)
        with writer._open_record_dir(root_path, top, parts, relative) as directory:
            return writer._read(root_path, directory, name, relative)[0]


def _row(entry):
    return {"id": entry.id, "kind": entry.kind, "name": entry.name, "status": entry.status,
            "client": entry.client, "valid": entry.valid}


def _record_rows(discovery):
    return [_row(e) for e in discovery.entries if e.id is not None]


def _project_rows(discovery):
    """``(rows, incomplete)`` for automatic folder matching.

    ``rows`` are the project dicts ``binding`` matches on. ``incomplete`` names the
    library-relative paths whose part in a folder match is unknown, and automatic
    binding refuses a single match while it is non-empty:

    - a record discovery could not read: it might be a project;
    - the whole library, when the walk stopped at its traversal budget: everything
      it did not reach might be a project that lists the folder too;
    - an entry whose kind or id could not be determined: it might be a project, or a
      project this row cannot name;
    - a project whose status could not be read: it might be active;
    - an active project whose ``locations`` did not arrive intact: its folders are
      unknown. They arrive from the same read that produced the entry, so a
      duplicated key, an unsupported YAML value, a value of another type or a
      list holding a non-string are all unknown, and no file is opened twice.

    Inactive projects contribute no locations, as before, so they are not read at all.
    Oversize files are excluded on purpose, like symlinks and dot-directories: the
    contract caps a record at 1 MiB, so discovery reports one as "can never be a
    record" and doctor flags it, and treating it as a match would block every folder
    in a library that happens to hold one large markdown file.
    """
    rows, incomplete = [], set(discovery.unreadable)
    if discovery.incomplete:
        # The walk stopped before it had listed every entry. Naming the root says
        # which part of the library is in doubt: all of it, past the point it read.
        incomplete.add(library_module.ROOT_RELPATH)
    for entry in discovery.entries:
        if entry.kind is None:
            incomplete.add(entry.relpath)  # disagreeing kind markers: could be a project
            continue
        if entry.kind != "project":
            continue
        if entry.id is None or entry.status is None:
            incomplete.add(entry.relpath)
            continue
        if entry.status == "active":
            locations = discovery.project_locations.get(entry.relpath)
            if locations is None:
                incomplete.add(entry.relpath)
                continue
        else:
            locations = ()
        rows.append({"id": entry.id, "client": entry.client, "name": entry.name, "status": entry.status,
                     "valid": entry.valid, "locations": list(locations)})
    return rows, tuple(sorted(incomplete))


def _by_name(items):
    return sorted(items, key=lambda item: (normalize_text(item["name"] or ""), item["id"]))


def _unreadable_paths(state):
    """The library-relative records a resolution could not read, bounded for one response."""
    return list(state.unreadable[:UNREADABLE_CAP])


def _state_error(state, labels=None):
    reason = binding_reason_text(state.reason, labels)
    return EngagementsError("invalid_binding", f"Cannot bind {state.reason_record}: {reason}. Nothing was bound.",
                            {"reason": state.reason, "record": state.reason_record})


def _invalid_binding_error(reason, record, labels=None):
    return EngagementsError("invalid_binding",
                            f"Cannot bind {record}: {binding_reason_text(reason, labels)}. Nothing was bound.",
                            {"reason": reason, "record": record})


def binding_reason_text(reason, labels=None):
    """Why a binding no longer holds, as the model reads it.

    ``client_changed`` names the entity, so it takes the configured label; every other
    reason describes the record itself and names nothing the settings rename.
    """
    if reason == "client_changed":
        client_word, _ = binding.display_words(labels)
        return f"its {client_word} changed"
    return _REASON_TEXT.get(reason, "it cannot be used")


def _stored(lib, session):
    if not session.session_id:
        return None
    return binding.load_binding(session.state_get, lib.library_id, session.session_id)


def _stored_mode(stored):
    """The mode a stored binding declares, or None when nothing usable is stored.

    A corrupt value is not a mapping, and asking it for a mode would raise. It resolves to no
    mode at all, exactly like an absent binding, so the invalid stored value is reported as
    ``invalid_binding`` / ``stored_unreadable`` instead of failing the call.
    """
    return stored.get("mode") if isinstance(stored, Mapping) else None


def _auto_state(lib, discovery, session, stored, state):
    """The live automatic resolution, reported independently of what is stored.

    ``state`` already is that resolution when nothing is stored, or when what is stored is a
    well-formed automatic binding whose retained identity is part of it. A stored value that
    cannot be read at all is different: ``state`` is then the refusal that value caused, and
    reporting it as the automatic result would say the folder match failed when it did not.
    So that one - like an explicit binding - is resolved again from the folder alone.
    """
    if state.reason == "stored_unreadable" or _stored_mode(stored) not in (None, "auto"):
        return _resolve_state(lib, discovery, session, None)
    return state


def _resolve_state(lib, discovery, session, stored):
    config = _config(session)
    mode = _stored_mode(stored)
    projects, incomplete = _project_rows(discovery) if mode in ("auto", None) else ([], ())
    rows = _record_rows(discovery)
    return binding.resolve_binding(stored, projects, rows, session.cwd, session.surface,
                                   auto_bind=config.auto_bind, incomplete=incomplete, labels=config.labels)


@dataclass(frozen=True)
class _ReadRecord:
    """One record's bytes with every fact derived from those bytes."""

    relpath: str
    data: bytes
    rev: str
    parsed: object
    entry: object
    library_defects: frozenset = frozenset()


def _read_authoritative(lib, discovery, session, wanted, warnings):
    """Read each wanted id once: ``{id: _ReadRecord or None}``, keyed by the discovered id.

    A caller that resolves by id and then reports or persists a binding must not
    mix discovery rows with a later read: the file can change in between and the
    response then describes one version while returning another's rev.
    ``None`` means the file is gone. ``library_defects`` carries the
    library-wide facts (a duplicated id, an unknown client) of these bytes, so
    the pair check sees the same defects discovery would have.
    """
    read = {}
    for entry in discovery.entries:
        if entry.id in wanted and entry.id not in read:
            try:
                data, revision = writer.read_record(lib, entry.relpath, clock=session.clock, warnings=warnings)
            except EngagementsError as exc:
                if exc.code != "not_found":
                    raise
                read[entry.id] = None
                continue
            parsed = records.parse(data)
            read[entry.id] = _ReadRecord(
                entry.relpath, data, revision, parsed,
                library_module._entry(entry.relpath, data, parsed),
                frozenset(lib.library_defects(parsed, entry.relpath, discovery=discovery)))
    for record_id in wanted:
        read.setdefault(record_id, None)
    return read


def _wanted_pair(state):
    wanted = {}
    if state.client_id is not None:
        wanted[state.client_id] = "client"
    if state.project_id is not None:
        wanted[state.project_id] = "project"
    return wanted


def _drift(records_read, wanted, client_id):
    """Why the resolved binding no longer holds, and the record it names.

    The resolved pair is validated against the bytes that will be reported:
    identity, kind, defects, status, and that the project still names that
    client. Nothing here resolves again, so a changed library can neither
    substitute another client or project nor turn a changed binding into a
    different, successful one.
    """
    for record_id, kind in wanted.items():
        item = records_read.get(record_id)
        if item is None:
            return "record_missing", record_id
        if item.entry.id != record_id or item.entry.kind != kind:
            return "record_identity_changed", record_id
        if item.entry.defects or item.library_defects:
            return "record_malformed", record_id
        if item.entry.status != "active":
            return "record_inactive", record_id
    project_id = next((record_id for record_id, kind in wanted.items() if kind == "project"), None)
    if project_id is not None and records_read[project_id].entry.client != client_id:
        return "client_changed", project_id
    return None, None


def _state_with_observations(state, records_read):
    """The same resolved state, with every reported fact taken from its bytes."""
    client = records_read.get(state.client_id)
    project = records_read.get(state.project_id) if state.project_id is not None else None
    return replace(state,
                   client_name=None if client is None else client.entry.name,
                   project_name=None if project is None else project.entry.name)


def _settle_binding(lib, discovery, session, stored, warnings):
    """Resolve once, then validate that pair against the bytes it will report.

    A second resolution is never run: re-deciding the binding after discovery
    let a reparented or deactivated record select another client and persist it.
    Returns the settled state and the records read for it.
    """
    state = _resolve_state(lib, discovery, session, stored)
    if state.state != "bound":
        return state, {}
    wanted = _wanted_pair(state)
    records_read = _read_authoritative(lib, discovery, session, wanted, warnings)
    reason, record = _drift(records_read, wanted, state.client_id)
    if reason is not None:
        raise _invalid_binding_error(reason, record, _config(session).labels)
    return _state_with_observations(state, records_read), records_read


# --- find ----------------------------------------------------------------------------------


def _find(args, session, warnings):
    _validate_find(args)
    lib = _open(session, warnings)
    result = lib.find(args["query"], args.get("kind"), args.get("include_inactive", False), args.get("limit", 20),
                      args.get("offset", 0))
    _add_warnings(warnings, result.warnings)
    return result.data()


# --- bind ----------------------------------------------------------------------------------


def _capped_text(data, section):
    body = data[section.body_start:section.end].decode("utf-8", errors="replace").replace("\r\n", "\n")
    lines = body.split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    text = "\n".join(lines)
    if len(text) <= SECTION_TEXT_CAP:
        return text, False
    window = text[:SECTION_TEXT_CAP]
    cut = window.rfind("\n")
    return (window[:cut] if cut >= 0 else window), True


def _bound_record(records_read, record_id, *, sections):
    """One member of the settled pair, from the bytes the pair was validated on.

    There is no fallback read: every bound record comes from the validated pair,
    so a record that was not read cannot be described. The record's own name, each
    section title and each location are clipped the way a context read clips them,
    and a clipped part says so: this is a summary, and a reader that needs the
    rest reads the record.
    """
    item = records_read.get(record_id)
    if item is None:
        raise _invalid_binding_error("record_missing", record_id)
    data, revision, parsed, usable, relpath = (item.data, item.rev, item.parsed, item.entry, item.relpath)
    result = {"id": usable.id, "name": usable.name, "status": usable.status, "rev": revision, "path": relpath}
    if isinstance(result["name"], str):
        result["name"], cut = _clip(result["name"], RELATED_NAME_CAP)
        if cut:
            result["truncated"] = True
    if sections:
        out = []
        for key in BIND_SECTIONS:
            found = next((s for s in parsed.sections if s.key == key), None)
            if found is not None:
                text, partial = _capped_text(data, found)
                title, cut = _clip(found.title, SECTION_TITLE_CAP)
                row = {"title": title, "text": text, "partial": partial}
                if cut:
                    row["truncated"] = True
                out.append(row)
        result["sections"] = out
    else:
        value = (parsed.properties or {}).get("locations")
        listed = [i for i in value if isinstance(i, str)] if isinstance(value, list) else []
        kept, clipped = [], len(listed) > RELATED_LOCATIONS_CAP
        for location in listed[:RELATED_LOCATIONS_CAP]:
            text, cut = _clip(location, RELATED_LOCATION_CAP)
            kept.append(text)
            clipped = clipped or cut
        result["locations"] = kept
        if clipped:
            result["truncated"] = True
    return result


def _bind_data(lib, discovery, state, records_read):
    client = project = None
    active, active_total = [], 0
    if state.state == "bound" and state.client_id is not None:
        client = _bound_record(records_read, state.client_id, sections=True)
        if state.project_id is not None:
            project = _bound_record(records_read, state.project_id, sections=False)
        # Every related row from its own bytes, like context's ``projects``: a
        # project deleted, reparented or deactivated after discovery is not listed
        # under this client.
        rows = _by_name([{"id": usable.id, "name": usable.name, "status": usable.status}
                         for usable, _ in _related_projects(lib, discovery, state.client_id, records_read)
                         if usable.status == "active"])
        active_total = len(rows)
        active = [_bounded_related_row(row) for row in rows[:CONTEXT_PROJECTS_CAP]]
    data = {"mode": state.mode, "state": state.state, "method": state.method, "evidence": state.evidence,
            "client": client, "project": project, "active_projects": active,
            "candidates": [dict(c) for c in state.candidates] if state.state == "ambiguous" else []}
    if active_total > len(active):
        data["active_projects_total"] = active_total
        data["active_projects_truncated"] = True
    if state.unreadable:  # the same reason the error and the banner carry, as paths
        data["unreadable"] = _unreadable_paths(state)
    return data


def _no_match_error(ref, labels):
    """The refusal for a reference that matches no record, in the configured words."""
    client_word, project_word = binding.display_words(labels)
    return EngagementsError("not_found", f"No {client_word} or {project_word} matches '{ref}'.",
                            {"reason": "no_match"})


def _resolve(lib, ref, kind, labels, *, discovery, client=None):
    """``lib.resolve`` with its refusal worded at this boundary.

    The library resolves references and phrases its own ``not_found``; the configured
    labels are known here, so the one message the model reads is rendered with them.
    The code, reason and every other error stay the library's own.
    """
    try:
        return lib.resolve(ref, kind, client=client, discovery=discovery)
    except EngagementsError as exc:
        if exc.code == "not_found" and exc.details.get("reason") == "no_match":
            raise _no_match_error(ref, labels) from None
        raise


def _resolve_targets(lib, discovery, args, labels):
    client_entry = project_entry = None
    if args.get("client") is not None:
        client_entry = _resolve(lib, args["client"], "client", labels, discovery=discovery)
    if args.get("project") is not None:
        scope = client_entry.id if client_entry is not None else None
        project_entry = _resolve(lib, args["project"], "project", labels,
                                 discovery=discovery, client=scope)
        if client_entry is None:
            client_entry = next((e for e in discovery.entries if e.kind == "client" and e.id == project_entry.client),
                                None)
        elif project_entry.client != client_entry.id:
            # The sentence opens with the entity name, so it takes the label's own case.
            project_word = binding.declared_labels(labels)["project"]
            raise EngagementsError(
                "invalid_binding",
                f"{project_word} {project_entry.id} belongs to {project_entry.client}, not {client_entry.id}. "
                "Nothing was bound.",
                {"project": project_entry.id, "project_client": project_entry.client,
                 "requested_client": client_entry.id})
    return client_entry, project_entry


def _bind(args, session, warnings):
    mode = _validate_bind(args)
    if not session.session_id:
        raise EngagementsError("no_session", "This call has no session id, so there is nothing to bind to.", {})
    lib = _open(session, warnings)
    discovery = lib.discover()
    if mode == "explicit":
        client_entry, project_entry = _resolve_targets(lib, discovery, args, _config(session).labels)
        owner = client_entry if client_entry is not None else project_entry
        client_id = owner.id if owner is client_entry else owner.client
        value = binding.explicit_binding(client_id, None if project_entry is None else project_entry.id,
                                         clock=session.clock, labels=_config(session).labels)
    elif mode == "off":
        value = {"mode": "off"}
    else:
        value = {"mode": "auto"}
    # Resolve, then revalidate and re-resolve against the bytes the response will
    # report, so discovery cannot persist or describe a stale record.
    state, records_read = _settle_binding(lib, discovery, session, value, warnings)
    if mode == "auto":
        value = binding.stored_after(value, state)
    if state.state == "invalid_binding":
        raise _state_error(state, _config(session).labels)
    data = _bind_data(lib, discovery, state, records_read)
    binding.store_binding(session.state_set, lib.library_id, session.session_id, value)
    return data


# --- context -------------------------------------------------------------------------------


def _unbound_error(lib, discovery, session, stored, kind, state=None):
    """The ``unbound`` error for ``kind``.

    ``state`` is the resolution that refused the read, when the caller has one.
    Reusing it keeps the error's own explanation in step with the decision: the
    library can recover between two reads, and a second resolution would then
    describe a ``bound`` state that never refused anything. ``details.auto`` is
    still the live automatic resolution, reported independently of the stored
    mode (spec 6.4), so that one is resolved on its own when the binding is
    explicit.
    """
    config = _config(session)
    client_word, project_word = binding.display_words(config.labels)
    message = f"No id or path was given and no {client_word} or {project_word} is bound to this session."
    if not session.session_id:
        auto = {"state": "unbound", "reason": "no_session", "evidence": "no session", "candidates": []}
        top = {"mode": "auto", **auto}
    else:
        state = _resolve_state(lib, discovery, session, stored) if state is None else state
        auto_state = _auto_state(lib, discovery, session, stored, state)
        auto = {"state": auto_state.state, "reason": auto_state.reason, "evidence": auto_state.evidence,
                "candidates": [dict(c) for c in auto_state.candidates]}
        if auto_state.unreadable:
            auto["unreadable"] = _unreadable_paths(auto_state)
        reason = state.reason
        shown = state.state
        if state.state == "bound" and state.project_id is None and kind == "project":
            reason = "no_project"
        top = {"mode": state.mode, "state": shown, "reason": reason, "evidence": state.evidence,
               "candidates": [dict(c) for c in state.candidates]}
        if state.unreadable:
            top["unreadable"] = _unreadable_paths(state)
        if state.reason == "stored_unreadable":
            # The stored binding is corrupt, not the records: say what to do about it, in the
            # words the banner uses for the same state, so the model can repair it either way.
            pick = f"a {client_word}" if client_word == "client" else client_word
            message = (f"The stored binding is unreadable. Nothing is bound; do not rely on {client_word} context "
                       f"from earlier turns. Pick {pick} with engagements_bind.")
    del config
    return EngagementsError("unbound", message, {**top, "auto": auto})


def _bound_target(lib, discovery, session, kind):
    """The bound entry of ``kind`` and the state that chose it, or ``unbound`` (stage 3)."""
    stored = _stored(lib, session)
    state = None
    if stored is not None or session.session_id:
        state = _resolve_state(lib, discovery, session, stored)
        if state.state == "bound":
            wanted = state.client_id if kind == "client" else state.project_id
            if wanted is not None:
                entry = next((e for e in discovery.entries if e.id == wanted), None)
                if entry is not None:
                    return entry, state
    raise _unbound_error(lib, discovery, session, stored, kind, state)


@dataclass(frozen=True)
class _ReadTarget:
    """One record read, plus the identity the caller resolved for it.

    ``bound`` is the binding state that selected the record when neither an id nor
    a path was given; otherwise the resolved id and kind are what the bytes must
    still carry.
    """

    relpath: str
    data: bytes
    rev: str
    parsed: Any
    expected_id: str | None = None
    expected_kind: str | None = None
    bound: Any = None
    observed: Any = None


def _read_target(lib, discovery, session, args, warnings):
    """Stage 3 and the read together, so the identity and the bytes come from one place.

    Resolving an id against discovery and then reading a path is two observations
    of the library. Carrying the resolved identity alongside the bytes lets
    ``_context`` refuse a swap between them instead of returning another record.
    A bound target arrives with both members of its binding already read and
    validated.
    """
    bound = None
    if args.get("id") is not None:
        entry = _resolve(lib, args["id"], None, _config(session).labels, discovery=discovery)
        relpath, expected_id, expected_kind = entry.relpath, entry.id, entry.kind
    elif args.get("path") is not None:
        relpath, expected_id, expected_kind = lib.record_path(args["path"]), None, None
    else:
        kind = args.get("kind", "client")
        entry, bound = _bound_target(lib, discovery, session, kind)
        observed = _settle_bound_read(lib, discovery, session, bound, warnings)
        record_id = bound.client_id if kind == "client" else bound.project_id
        item = observed[record_id]
        return _ReadTarget(item.relpath, item.data, item.rev, item.parsed, record_id, kind, bound, observed)
    data, revision = writer.read_record(lib, relpath, clock=session.clock, warnings=warnings)
    return _ReadTarget(relpath, data, revision, records.parse(data), expected_id, expected_kind, bound)


def _settle_bound_read(lib, discovery, session, state, warnings):
    """Read and validate the complete bound pair before a bound read is returned.

    Reading only the requested member let a bound client read keep listing a
    project after it was reparented, and let a bound project read ignore its
    client going former. Drift refuses as
    ``unbound``/``invalid_binding`` and nothing is returned.
    """
    wanted = _wanted_pair(state)
    records_read = _read_authoritative(lib, discovery, session, wanted, warnings)
    reason, _ = _drift(records_read, wanted, state.client_id)
    if reason is not None:
        raise _bound_invalid(lib, discovery, session, state, reason)
    return records_read


def _bound_invalid(lib, discovery, session, state, reason):
    """The unbound error a bound read returns when the binding no longer holds (spec 6.4)."""
    auto_state = state if state.mode == "auto" else _resolve_state(lib, discovery, session, None)
    auto = {"state": auto_state.state, "reason": auto_state.reason, "evidence": auto_state.evidence,
            "candidates": [dict(c) for c in auto_state.candidates]}
    if auto_state.unreadable:
        auto["unreadable"] = _unreadable_paths(auto_state)
    top = {"mode": state.mode, "state": "invalid_binding", "reason": reason,
           "evidence": f"invalid: {reason}", "candidates": []}
    client_word, project_word = binding.display_words(_config(session).labels)
    return EngagementsError("unbound",
                            f"No id or path was given and no {client_word} or {project_word} is bound to this session.",
                            {**top, "auto": auto})


def _defect_message(kind, key=None):
    # The key is file content: a flagged one is replaced whole before it is put
    # into prose, so no part of it survives the span redactor. What is kept of a long
    # one is cut after redaction, never before it.
    if not key:
        return f"Record defect: {kind}."
    label, _cut = _clip(redact_label(key), WARNING_LABEL_CAP)
    return f"Record defect: {kind} on '{label}'."


def _duplicate_message(key):
    label, _cut = _clip(redact_label(key), WARNING_LABEL_CAP)
    return f"Duplicate section: '{label}'."


def _defect_warnings(path, parsed, library_defects):
    out = []
    global_defects = set(parsed.global_defects) | set(library_defects)
    for kind in sorted(global_defects):
        out.append({"code": "record_invalid", "message": _defect_message(kind), "path": path})
    for key in sorted(parsed.property_defects):
        out.append({"code": "record_invalid", "message": _defect_message(parsed.property_defects[key], key),
                    "path": path})
    for key in sorted(parsed.section_defects):
        out.append({"code": "record_invalid", "message": _defect_message(parsed.section_defects[key], key),
                    "path": path})
    for key in sorted(parsed.section_defects):
        out.append({"code": "duplicate_section", "message": _duplicate_message(key), "path": path})
    if "encoding" in global_defects:
        out.append({"code": "encoding_invalid", "message": "The record is not valid UTF-8.", "path": path})
    return _bounded_warnings(out)


def _omitted_defects(count, path):
    """The one entry that says how many diagnostics a response does not list."""
    return {"code": "record_defects_omitted", "omitted": count,
            "message": f"{count} further record defects are not listed.", "path": path}


def _bounded_warnings(items):
    """At most :data:`CONTEXT_WARNINGS_CAP` diagnostics, plus one count of the rest."""
    if len(items) <= CONTEXT_WARNINGS_CAP:
        return items
    kept = items[:CONTEXT_WARNINGS_CAP]
    kept.append(_omitted_defects(len(items) - CONTEXT_WARNINGS_CAP, items[-1]["path"]))
    return kept


def _give_up_diagnostic(warnings):
    """Drop one diagnostic, keeping the count of the ones not listed truthful.

    The list a read returns ends with the count of everything it left out, so giving
    a diagnostic up merges it into that count instead of losing it. ``False`` when
    there is nothing left to give up: the count alone is the last thing a response
    keeps.
    """
    listed = [item for item in warnings if item.get("code") != "record_defects_omitted"]
    if not listed:
        return False
    counted = sum(item.get("omitted", 0) for item in warnings
                  if item.get("code") == "record_defects_omitted")
    warnings[:] = listed[:-1]
    warnings.append(_omitted_defects(counted + 1, listed[-1].get("path")))
    return True


def _span_text(data, section):
    text = data[section.start:section.end].decode("utf-8", errors="replace").replace("\r\n", "\n")
    return text if text.endswith("\n") else text + "\n"


def _build_view(data, parsed, requested):
    """The paginated view and where each selected section lies in it."""
    if requested is None:
        chosen = list(parsed.sections)
    else:
        keys = {section_key(title) for title in requested}
        chosen = [s for s in parsed.sections if s.key in keys]
    view, placed = "", []
    for section in chosen:
        text = _span_text(data, section)
        heading = text.find("\n") + 1
        placed.append((section, len(view), len(view) + heading, len(view) + len(text)))
        view += text
    return view, placed


def _json_bytes(value):
    """The size of ``value`` as JSON, ASCII-escaped: the larger of the two encodings."""
    return len(json.dumps(value, ensure_ascii=True).encode("utf-8"))


def _wire_bytes(value):
    """The size of ``value`` in the bytes the transport writes.

    The runtime serializes a result with ``ensure_ascii=False``, so a non-ASCII
    character costs its UTF-8 length, but JSON still escapes control characters: a
    NUL in a record's text is six bytes either way.
    """
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def _response_bytes(data, warnings):
    """The size of the envelope this tool returns, before it is written out.

    Everything the record supplies is measured ASCII-escaped, the larger of the two
    encodings, so a bound met here is met however the transport escapes it. The one
    exception is ``missing_sections``: those titles are the caller's own request
    echoed back, and the request schema already bounds them, so they are measured in
    the bytes the runtime writes rather than in an escape of them.
    """
    envelope = {"ok": True, "data": data, "warnings": warnings}
    size = _json_bytes(envelope)
    echoed = data.get("missing_sections") if isinstance(data, dict) else None
    if isinstance(echoed, list) and echoed:
        # The echoed titles are one contiguous JSON value inside the envelope, so
        # replacing their escaped size with their written size is exact.
        size -= _json_bytes(echoed) - _wire_bytes(echoed)
    return size


def _window_end(view, offset):
    """Where the page window starting at ``offset`` ends, snapped to a line.

    Up to :data:`PAGE_SIZE` code points. If the window does not reach the end of the
    view it ends just after the last ``\\n`` inside it, unless it holds none (a
    single line longer than a page), in which case it is the window's own end.
    """
    end = min(offset + PAGE_SIZE, len(view))
    if end < len(view):
        cut = view.rfind("\n", offset, end)
        if cut >= 0:
            end = cut + 1
    return end


def _shrink(view, offset, end):
    """A shorter page end: a line boundary when one is close, and always progress."""
    if end <= offset + 1:
        return offset
    shorter = max(offset + 1, end - CONTEXT_SHRINK_STEP)
    cut = view.rfind("\n", max(offset, shorter - CONTEXT_SHRINK_SNAP), shorter)
    if cut >= offset:
        shorter = cut + 1
    return min(end - 1, max(offset + 1, shorter))


def _overlapping(placed, offset, end):
    """The sections whose span meets ``[offset, end)``, in view order."""
    return [row for row in placed if row[3] > offset and row[1] < end]


def _section_row(view, offset, end, section, start, body, stop):
    title, cut = _clip(section.title, SECTION_TITLE_CAP)
    item = {"title": title, "text": view[max(offset, body):min(end, stop)] if end > body else "",
            "partial": not (start >= offset and stop <= end)}
    if cut:
        item["truncated"] = True
    return item


def _context_page(base, view, placed, offset, warnings, raw=False):
    """The page, its section rows and where the next page starts.

    The page starts as the window from ``offset``. It ends early at a section
    boundary when the index would hold more than :data:`CONTEXT_SECTIONS_CAP` rows,
    and is shrunk until the whole envelope fits :data:`CONTEXT_RESPONSE_BYTES`: whole
    sections are given up while more than one is on the page, and only a page that
    is one section, or none, is cut inside its text. ``next_offset`` is where the
    page really ends, so a reader that follows it reads every section body; the
    headings the page window covered but the page does not list are counted in
    ``sections_total`` with ``sections_truncated: true``.

    A record that is not valid UTF-8 has no sections to index, and its page is the
    ``raw_text`` the response carries. That text is measured inside the same
    envelope as a section page, so a page of text that JSON escapes is cut until the
    response fits, and the cut never stalls the continuation: a page always covers
    at least one code point.
    """
    covered = len(_overlapping(placed, offset, _window_end(view, offset)))
    limit = CONTEXT_RESPONSE_BYTES - CONTEXT_RESPONSE_MARGIN
    end = _window_end(view, offset)
    while True:
        rows = _overlapping(placed, offset, end)
        if len(rows) > CONTEXT_SECTIONS_CAP:
            end = max(offset, rows[CONTEXT_SECTIONS_CAP][1])
            rows = rows[:CONTEXT_SECTIONS_CAP]
        sections = [_section_row(view, offset, end, *row) for row in rows]
        result = dict(base)
        result["sections"] = sections
        result["raw_text"] = view[offset:end] if raw else None
        result["offset"] = offset
        result["next_offset"] = end if end < len(view) else None
        result["view_length"] = len(view)
        if covered > len(sections):
            result["sections_total"] = covered
            result["sections_truncated"] = True
        if _response_bytes(result, warnings) <= limit or end <= offset:
            return result
        if len(rows) > 1 and rows[-1][1] > offset:
            end = rows[-1][1]  # give up the last whole section
        else:
            end = _shrink(view, offset, end)


def _clip(text, limit):
    """``(text, clipped)``: the first ``limit`` characters, and whether any were dropped."""
    return (text, False) if len(text) <= limit else (text[:limit], True)


def _bounded_property_value(value, limit):
    """One property value with every string clipped and every list cut; ``(value, clipped)``.

    Strings are clipped item by item, and a list keeps at most
    :data:`PROPERTY_LIST_CAP` items, so a list of twenty thousand one-character
    values cannot arrive as twenty thousand items. Any other JSON value the parser
    produces (a number, a boolean, null, the ``unsupported_yaml`` placeholder) is
    already small and passes through.
    """
    if isinstance(value, str):
        return _clip(value, limit)
    if isinstance(value, list):
        items, clipped = [], len(value) > PROPERTY_LIST_CAP
        for item in value[:PROPERTY_LIST_CAP]:
            if isinstance(item, str):
                text, cut = _clip(item, limit)
                items.append(text)
                clipped = clipped or cut
            else:
                items.append(item)
        return items, clipped
    return value, False


def _bounded_properties(properties):
    """``(properties, clipped_keys, omitted)``: the copy one response returns.

    Every string is clipped, every list keeps at most :data:`PROPERTY_LIST_CAP`
    items, every key is kept whole and no longer than :data:`PROPERTY_KEY_CAP`, at
    most :data:`CONTEXT_PROPERTY_KEYS_CAP` keys are returned, and the serialized
    properties stay under :data:`CONTEXT_PROPERTIES_BYTES`. A key dropped for any of
    those reasons is counted in ``omitted``; a key whose value was cut is listed in
    ``clipped_keys``. Property names are otherwise kept exactly as the record holds
    them. The record on disk is untouched.
    """
    if not isinstance(properties, dict):
        return properties, [], 0
    out, clipped = {}, []
    omitted = 0
    used = 2  # the braces
    admitted_over = False
    for key, value in properties.items():
        if len(out) >= CONTEXT_PROPERTY_KEYS_CAP or len(key) > PROPERTY_KEY_CAP:
            omitted += 1
            continue
        bounded, cut = _bounded_property_value(value, PROPERTY_VALUE_CAP)
        size = _json_bytes(key) + 1 + _json_bytes(bounded)
        if used + size > CONTEXT_PROPERTIES_BYTES and size > PROPERTY_SMALL_BYTES:
            if admitted_over:
                omitted += 1
                continue
            admitted_over = True  # one value that does not fit is still worth returning
        out[key] = bounded
        used += size
        if cut:
            clipped.append(key)
    return out, clipped, omitted


def _bounded_related_row(row):
    """One related project row with its name and locations bounded.

    A row whose name was clipped, whose locations were clipped, or which kept fewer
    locations than its record lists, carries ``truncated: true``: the row is a
    summary, and a reader that needs the rest reads the project's record.
    """
    out = dict(row)
    clipped = False
    if isinstance(out.get("name"), str):
        out["name"], cut = _clip(out["name"], RELATED_NAME_CAP)
        clipped = clipped or cut
    locations = out.get("locations")
    if isinstance(locations, list):
        kept = []
        for location in locations[:RELATED_LOCATIONS_CAP]:
            if isinstance(location, str):
                text, cut = _clip(location, RELATED_LOCATION_CAP)
                kept.append(text)
                clipped = clipped or cut
            else:
                kept.append(location)
        clipped = clipped or len(locations) > RELATED_LOCATIONS_CAP
        out["locations"] = kept
    if clipped:
        out["truncated"] = True
    return out


def _bounded_projects(rows):
    """``(rows, truncated)``: the related rows one response returns, all bounded.

    At most :data:`CONTEXT_PROJECTS_CAP` rows and at most
    :data:`CONTEXT_RELATED_BYTES` of them together, in the list's own order. Rows the
    budget leaves out are simply absent, and the caller marks that with
    ``projects_total`` and ``projects_truncated``.
    """
    kept, used = [], 2
    for row in rows[:CONTEXT_PROJECTS_CAP]:
        bounded = _bounded_related_row(row)
        size = _json_bytes(bounded) + 1
        if kept and used + size > CONTEXT_RELATED_BYTES:
            break
        kept.append(bounded)
        used += size
    return kept, len(kept) < len(rows)


def _context_base(lib, discovery, parsed, target, relpath, revision, invalid, requested):
    """Everything a read returns except the page, with every part already bounded."""
    properties, clipped_properties, omitted = _bounded_properties(parsed.properties)
    present = {section.key for section in parsed.sections}
    missing, seen = [], set()
    for title in requested or []:
        key = section_key(title)
        if key not in present and key not in seen:
            missing.append(title)
        seen.add(key)
    result = {"id": parsed.id, "kind": parsed.kind, "path": relpath, "library_root": str(lib.root),
              "rev": revision, "valid": not invalid, "properties": properties, "missing_sections": missing}
    if clipped_properties:
        result["properties_truncated"] = clipped_properties
    if omitted:
        result["properties_omitted"] = omitted
    if parsed.kind == "client":
        rows = _client_projects(lib, discovery, parsed.id, target.observed)
        kept, truncated = _bounded_projects(rows)
        result["projects"] = kept
        if truncated:
            result["projects_total"] = len(rows)
            result["projects_truncated"] = True
    elif parsed.kind == "project":
        result["client"] = _project_client(discovery, parsed, target.observed)
    return result


def _fit_base(result, warnings):
    """Trim the parts that are not the page until a page still fits inside the bound.

    Each part is already clipped, so this only fires on a combination of them, or on
    a path long enough to dominate the envelope. Rows, property keys, requested
    titles and then diagnostics are given up from the end, so what is kept is a
    prefix of the record's own order and every drop is counted in the response.
    """
    budget = CONTEXT_RESPONSE_BYTES - CONTEXT_RESPONSE_MARGIN - CONTEXT_PAGE_BYTES
    while _response_bytes(result, warnings) > budget:
        projects = result.get("projects")
        if projects:
            projects.pop()
            result["projects_truncated"] = True
            result.setdefault("projects_total", len(projects) + 1)
            continue
        properties = result.get("properties")
        if isinstance(properties, dict) and properties:
            key = next(reversed(properties))
            del properties[key]
            result["properties_omitted"] = result.get("properties_omitted", 0) + 1
            clipped = result.get("properties_truncated")
            if isinstance(clipped, list) and key in clipped:
                clipped.remove(key)
                if not clipped:
                    del result["properties_truncated"]
            continue
        if result.get("missing_sections"):
            result["missing_sections"].pop()
            result["missing_sections_omitted"] = result.get("missing_sections_omitted", 0) + 1
            continue
        if not _give_up_diagnostic(warnings):
            break
    return result


def _context(args, session, warnings):
    _validate_context(args)
    lib = _open(session, warnings)
    discovery = lib.discover()
    target = _read_target(lib, discovery, session, args, warnings)
    data, parsed = target.data, target.parsed
    if not parsed.is_record:
        raise EngagementsError("not_found", "Path is not a record.", {"reason": "not_a_record"})
    library_defects = lib.library_defects(parsed, target.relpath, discovery=discovery)
    if target.bound is None and target.expected_id is not None and (
            parsed.id, parsed.kind) != (target.expected_id, target.expected_kind):
        # The id resolved to this path, but the bytes now hold another record: never return
        # its body for the requested id. A bound read was checked as a pair before it
        # was read.
        client_word, project_word = binding.display_words(_config(session).labels)
        raise EngagementsError("not_found",
                               f"No {client_word} or {project_word} with id '{target.expected_id}'.",
                               {"reason": "no_match"})
    relpath = target.relpath
    revision = target.rev
    invalid = bool(parsed.global_defects or parsed.property_defects or parsed.section_defects or library_defects)
    _add_warnings(warnings, _defect_warnings(relpath, parsed, library_defects))

    raw = "encoding" in parsed.global_defects
    requested = args.get("sections")
    if raw:
        view, placed = data.decode("utf-8", errors="replace").replace("\r\n", "\n"), []
    else:
        view, placed = _build_view(data, parsed, requested)
    offset = args.get("offset", 0)
    if offset >= len(view) and not (offset == 0 and not view):
        raise _invalid("offset", "offset_out_of_range")
    if "rev" in args and args["rev"] != revision:
        raise EngagementsError(
            "conflict", "The record changed since your last read. Nothing was returned. Read it again from offset 0.",
            {"reason": "rev_changed", "current_rev": revision, "footprints": []})
    base = _fit_base(_context_base(lib, discovery, parsed, target, relpath, revision, invalid, requested), warnings)
    return _context_page(base, view, placed, offset, warnings, raw)


def _locations_from(parsed):
    value = (parsed.properties or {}).get("locations")
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _related_projects(lib, discovery, client_id, observed=None):
    """``(entry, parsed)`` for each project whose own bytes name ``client_id`` now.

    The one path for every list of a client's projects (context ``projects``,
    bind ``active_projects``). The discovery row only says where a project was;
    its bytes say whether it still exists, still carries the discovered id, and
    who owns it. A project that is gone, cannot be read, or names another client
    is not listed: nothing is reconstructed from discovery. Observations already
    made for a bound pair are reused rather than read again.
    """
    found = []
    for entry in discovery.entries:
        if entry.kind != "project" or entry.id is None:
            continue
        item = (observed or {}).get(entry.id)
        if item is not None:
            usable, parsed = item.entry, item.parsed
        else:
            try:
                data = _read_plain(lib, entry.relpath)
                parsed = records.parse(data)
            except Exception:  # noqa: BLE001 - gone or unreadable: not a current project, so not listed
                continue
            usable = library_module._entry(entry.relpath, data, parsed)
        if (usable.id, usable.kind) != (entry.id, "project") or usable.client != client_id:
            continue
        found.append((usable, parsed))
    return found


def _client_projects(lib, discovery, client_id, observed=None):
    """A client's projects, active and inactive, from each project's own bytes."""
    return _by_name([{"id": usable.id, "name": usable.name, "status": usable.status,
                      "locations": _locations_from(parsed)}
                     for usable, parsed in _related_projects(lib, discovery, client_id, observed)])


def _project_client(discovery, parsed, observed=None):
    client_id = (parsed.properties or {}).get("client")
    if not isinstance(client_id, str):
        return {"id": None, "name": None}
    item = (observed or {}).get(client_id)
    if item is not None and item.entry.id == client_id and item.entry.kind == "client":
        return _bounded_client_name(client_id, item.entry.name)
    owner = next((e for e in discovery.entries if e.kind == "client" and e.id == client_id), None)
    return _bounded_client_name(client_id, None if owner is None else owner.name)


def _bounded_client_name(client_id, name):
    """A project's client, with a long name clipped and the clip marked."""
    row = {"id": client_id, "name": name}
    if isinstance(name, str):
        row["name"], cut = _clip(name, RELATED_NAME_CAP)
        if cut:
            row["truncated"] = True
    return row


# --- create --------------------------------------------------------------------------------


def _create_error(error, args, labels):
    """The create refusals, worded at this boundary.

    The library decides the refusal and its details; the entity word in the two messages
    that name one is configuration, so those are rendered here with the configured labels.
    Every other error, and the code and details of these, stay exactly as the library
    returned them.
    """
    client_word, project_word = binding.display_words(labels)
    if error.code == "not_found" and error.details.get("field") == "client" and args.get("client") is not None:
        return EngagementsError(error.code, f"No {client_word} with id '{args['client']}'.",
                                dict(error.details))
    if error.code == "similar_exists" and args.get("name") is not None:
        kind_word = client_word if args.get("kind") == "client" else project_word
        message = (f"A {kind_word} named like '{args['name']}' already exists. "
                   "Use it, or pass allow_similar to create another.")
        return EngagementsError(error.code, message, dict(error.details))
    return error


def _create_warning(warning, args, labels):
    """The create warnings, with the one that names the owner in the configured word.

    The owner-inactive warning is the only one that names the entity: the library words it
    with its own default, so the word before the quoted id is replaced with the label and
    the rest of the sentence, including the id, is left as the library wrote it.
    """
    client = args.get("client")
    suffix = f"'{client}' is not active."
    if warning.get("code") == "record_invalid" and isinstance(client, str) \
            and str(warning.get("message", "")).endswith(suffix):
        declared = binding.declared_labels(labels)["client"]
        return {**warning, "message": f"{declared} {suffix}"}
    return warning


def _create(args, session, warnings):
    _validate_create(args)
    lib = _open(session, warnings)
    labels = _config(session).labels
    try:
        result = lib.create(args["kind"], args["name"], args.get("client"), args.get("properties"), args.get("sections"),
                            args.get("source", "conversation"), args.get("allow_similar", False),
                            profile=_profile(session), today=session.clock)
    except EngagementsError as exc:
        raise _create_error(exc, args, labels) from None
    _add_warnings(warnings, [_create_warning(warning, args, labels) for warning in result.warnings])
    _add_warnings(warnings, _secret_warnings(secret_findings(_record_text(lib, result.path))))
    return result.data()


# --- update --------------------------------------------------------------------------------


def _update(args, session, warnings):
    _validate_update(args)
    lib = _open(session, warnings)
    discovery = lib.discover()
    matches = [e for e in discovery.entries if e.id == args["id"]]
    if not matches:
        client_word, project_word = binding.display_words(_config(session).labels)
        raise EngagementsError("not_found", f"No {client_word} or {project_word} with id '{args['id']}'.",
                               {"reason": "no_match"})
    if len(matches) > 1:
        raise library_module._ambiguous(matches)
    entry = matches[0]
    if entry.kind is None:
        raise EngagementsError("malformed", "Record has defects; run doctor.",
                               {"path": entry.relpath, "defects": [{"kind": "duplicate_key", "key": "engagements"}]})
    clients = {e.id for e in discovery.entries if e.kind == "client" and e.id is not None}
    before = _record_text(lib, entry.relpath)
    result = writer.update(
        lib, entry.relpath, args["base_rev"], args["source"], args["ops"], kind=entry.kind, expected_id=entry.id,
        profile=_profile(session), clock=session.clock, client_exists=lambda client: client in clients,
        library_defects=lambda record: lib.library_defects(record, entry.relpath, discovery=discovery))
    _add_warnings(warnings, result.warnings)
    _add_warnings(warnings, _secret_warnings(new_findings(before, _record_text(lib, entry.relpath))))
    return result.data


# --- session binding for hooks and commands --------------------------------------------------


def binding_for_library(lib, session):
    """``(state, stored)`` for ``session`` resolved against an already-opened ``lib``.

    Nothing is opened, created or written. A caller that already holds a library must use
    this rather than ``session_binding``, whose open honours the config's
    ``create_default`` and could recreate a library that vanished in between
    The dashboard opens once and resolves here.
    """
    discovery = lib.discover()
    stored = _stored(lib, session)
    return _resolve_state(lib, discovery, session, stored), stored


def session_binding(session, warnings=None):
    """The session's current binding, for the banner hook and the slash commands.

    Returns ``(library, state, stored)``: the opened library, the
    ``BindingState`` resolved against the library's current records (the same
    resolution ``engagements_bind`` and ``engagements_context`` start from), and
    the value held under the session's key. Unlike a tool call it never raises
    ``invalid_binding`` for a drifted record: an invalid binding is a state the
    banner and ``/client`` report. It raises ``EngagementsError`` only when the
    library cannot be opened or the state store fails, and it writes nothing.
    """
    warnings = [] if warnings is None else warnings
    lib = _open(session, warnings)
    state, stored = binding_for_library(lib, session)
    return lib, state, stored


# --- entry points --------------------------------------------------------------------------


def handle_find(args, session):
    return _run(_find, args, session)


def handle_bind(args, session):
    return _run(_bind, args, session)


def handle_context(args, session):
    return _run(_context, args, session)


def handle_create(args, session):
    return _run(_create, args, session)


def handle_update(args, session):
    return _run(_update, args, session)


HANDLERS = {
    "engagements_find": handle_find,
    "engagements_bind": handle_bind,
    "engagements_context": handle_context,
    "engagements_create": handle_create,
    "engagements_update": handle_update,
}


def call_tool(name, args, session):
    """Run one tool by name. An unknown name is ``invalid_op`` on the field ``tool``."""
    handler = HANDLERS.get(name)
    if handler is None:
        return _failure("invalid_op", "Invalid argument.", {"field": "tool", "reason": "invalid_value"}, [],
                        _roots(session))
    return handler(args, session)


__all__ = ["HANDLERS", "MISSING", "OP_SCHEMAS", "Session", "SessionConfig", "TOOL_SCHEMAS", "binding_for_library",
           "call_tool", "session_binding", "handle_bind", "handle_context", "handle_create", "handle_find",
           "handle_update"]
