"""Read-only diagnostics for a library root (spec 6.6).

Doctor never writes: no directory, lock, history file or snapshot is created,
not even for a root whose ``.engagements/library.yaml`` is missing. Reads go
through the same held, no-follow directory handles the rest of the plugin uses,
and every filesystem failure becomes a finding instead of an exception.

Output never carries file contents, secret values or absolute paths: a finding's
``path`` is relative to the root with ``/`` separators, and its ``message`` is
plain English built from fixed text plus at most record ids and relative paths.
"""

import ntpath
import os
import posixpath
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import library as library_module
from . import records as records_module
from . import writer
from .binding import _canonical
from .errors import EngagementsError
from .fsutil import ROOT_CHANGED, _Dir, _NotRealDirectory, _Refusal
from .library import Library
from .redact import location_text, redact_label, redact_text, secret_findings
from .schema import WALK_ENTRY_LIMIT, WALK_ENTRY_WARNING

ROOT_DISPLAY = "."
LIBRARY_YAML = ".engagements/library.yaml"
_SEVERITY_ORDER = {"error": 0, "warning": 1, "notice": 2}
_IDENTITY_KEYS = frozenset({"engagements", "id", "name"})
_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{1,62}")
_CLOSING_HASHES = re.compile(r"\s#+$")
# The secret finding's message: fixed words, a kind from ``redact.KINDS`` and a
# structural location. Nothing taken from the record is formatted into it.
SECRET_FINDING_MESSAGE = ("This record appears to hold a credential ({kind}) at {location}. "
                          "Remove it from the record and rotate it if it was real; client records "
                          "are shared files and must never hold secrets. The value is not shown.")
_HEADING_MESSAGE = ("Heading ends in a # run the parser treats as part of the title because of Unicode whitespace; use ASCII spaces.")


# --- report model --------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    """One diagnostic. ``path`` is relative to the library root, never absolute.

    ``message``, ``key`` and ``section`` are redacted on construction: they can
    quote a heading, a frontmatter key or an id taken from the record, and one of
    those can be secret-shaped. Redacting here covers structured
    and plain-text output alike. ``path`` and ``line`` stay, so the finding still
    says where to look.

    A secret finding (``secret_shaped``) carries one field taken from the record,
    ``path``, which says which record it is in. Everything else in it is
    structural or fixed: ``line``, ``area``, ``section_index``, ``kind`` and
    ``occurrence`` (the 1-based position of the finding on its line), and a
    message built from those alone. It never sets ``key`` or ``section``.
    """

    severity: str
    code: str
    message: str
    path: str | None = None
    line: int | None = None
    key: str | None = None
    section: str | None = None
    area: str | None = None
    section_index: int | None = None
    kind: str | None = None
    occurrence: int | None = None

    def __post_init__(self):
        object.__setattr__(self, "message", redact_text(self.message))
        object.__setattr__(self, "key", redact_label(self.key))
        object.__setattr__(self, "section", redact_label(self.section))

    def as_dict(self):
        data = {"severity": self.severity, "code": self.code}
        for name in ("path", "line", "key", "section", "area", "section_index", "kind", "occurrence"):
            value = getattr(self, name)
            if value is not None:
                data[name] = value
        data["message"] = self.message
        return data


def _sort_key(finding):
    return (
        _SEVERITY_ORDER.get(finding.severity, len(_SEVERITY_ORDER)),
        "" if finding.path is None else finding.path,
        -1 if finding.line is None else finding.line,
        finding.code,
        "" if finding.key is None else finding.key,
        "" if finding.section is None else finding.section,
        0 if finding.occurrence is None else finding.occurrence,
    )


def _dedupe(findings):
    """Keep one finding per (code, path, key, section, line), in sorted order.

    A secret finding is also told apart by its kind and its occurrence on the line,
    so two findings on one line stay two.
    """
    seen = set()
    unique = []
    for finding in findings:
        identity = (finding.code, finding.path, finding.key, finding.section, finding.line,
                    finding.area, finding.section_index, finding.kind, finding.occurrence)
        if identity in seen:
            continue
        seen.add(identity)
        unique.append(finding)
    return unique


def _count_text(count, word):
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


@dataclass
class DoctorReport:
    """The result of one doctor run. Findings are sorted when the report is built."""

    root_display: str
    checked: int
    findings: list = field(default_factory=list)

    def __post_init__(self):
        self.findings = _dedupe(sorted(self.findings, key=_sort_key))

    @property
    def clean(self):
        """True when nothing is wrong: notices alone leave a library clean."""
        return not any(finding.severity in ("error", "warning") for finding in self.findings)

    def counts(self):
        result = {"error": 0, "warning": 0, "notice": 0}
        for finding in self.findings:
            if finding.severity in result:
                result[finding.severity] += 1
        return result

    def as_dict(self):
        return {
            "clean": self.clean,
            "checked": self.checked,
            "counts": self.counts(),
            "findings": [finding.as_dict() for finding in self.findings],
        }

    def render_text(self):
        noun = "record" if self.checked == 1 else "records"
        lines = [f"Engagements doctor: {self.root_display} ({self.checked} {noun} checked)"]
        for finding in self.findings:
            location = self.root_display if finding.path is None else finding.path
            if finding.line is not None:
                location = f"{location}:{finding.line}"
            lines.append(f"{finding.severity:<7} {finding.code}  {location}  {finding.message}")
        lines.append(self._summary())
        return "\n".join(lines)

    def _summary(self):
        counts = self.counts()
        if counts["error"] == 0 and counts["warning"] == 0:
            text = "Doctor: clean"
            if counts["notice"]:
                plural = "" if counts["notice"] == 1 else "s"
                text += f" ({counts['notice']} notice{plural})"
            return text
        parts = [_count_text(counts[name], name)
                 for name in ("error", "warning", "notice") if counts[name]]
        return "Doctor: " + ", ".join(parts)


# --- path helpers ---------------------------------------------------------------


def _relative(root, value):
    """``value`` as a root-relative slash path, or None when that is not possible."""
    if not isinstance(value, str) or not value:
        return None
    try:
        text = os.path.relpath(value, root) if os.path.isabs(value) else value
    except (OSError, TypeError, ValueError):
        return None
    text = text.replace("\\", "/")
    if text in ("", ".", "..") or text.startswith("../") or os.path.isabs(text):
        return None
    return text


def _line_at(data, offset):
    """The 1-based file line of a byte offset."""
    if offset is None:
        return None
    return data.count(b"\n", 0, offset) + 1


def _key_line(record, key):
    spans = record.property_spans.get(key)
    return _line_at(record.data, spans[0][0]) if spans else None


def _name(project_id, relpath):
    """A project's id for a message, or its relative path when the id is not one.

    Only a well-formed id is echoed: any other value could be a path or free
    text, and doctor's output never carries those.
    """
    if isinstance(project_id, str) and _ID_PATTERN.fullmatch(project_id):
        return project_id
    return relpath


# --- reading ------------------------------------------------------------------


def _read_bytes(library, relpath):
    """The record's bytes, through held no-follow handles. Raises on any failure."""
    with writer._opened_root(library) as (root_path, top):
        parts, name, relative = writer._resolve(root_path, relpath)
        with writer._open_record_dir(root_path, top, parts, relative) as directory:
            return writer._read(root_path, directory, name, relative)[0]


def _open_root(root):
    """Hold ``root`` without following a link and keep its identity: ``(top, identity)``.

    Doctor used to resolve (and open) the root more than once per run.
    A root replaced between those observations made the report describe a
    directory the caller never named, and a symlink swapped in for it sent the
    read outside the library. Every later read now goes through a ``Library``
    carrying this identity, so a replacement fails with ``root_changed`` instead.
    """
    top = _Dir.open_root(root, writable=False)  # doctor never writes: never deny another process's writes
    try:
        if os.path.realpath(top.path) != os.fspath(top.path):
            raise _NotRealDirectory("", ROOT_CHANGED)
        return top, top.identity()
    except BaseException:
        top.close()
        raise


def _metadata_state(top):
    """``("valid", id)``, ``("missing", None)`` or ``("invalid", None)``; creates nothing.

    Uses the library's own reader, which opens the file without following a link
    and applies the same size and format rules, so doctor's verdict cannot drift
    from what a real open would decide. It runs on the held root whose identity
    this run keeps, and it classifies the metadata directly instead of routing
    through ``open_library``: that opener takes (and can create) the library lock
    on its invalid-metadata path, and doctor must never write (TOOLS.md 96-98).
    """
    meta = top.open_dir(".engagements")
    if meta is None:
        return "missing", None
    try:
        try:
            identifier = library_module._read_library_yaml(Path(top.path), meta)
        except EngagementsError as exc:
            if exc.code != "library_missing":
                raise
            return "invalid", None
    finally:
        meta.close()
    return ("valid", identifier) if identifier is not None else ("missing", None)


# --- record defects -------------------------------------------------------------


def _property_message(code):
    if code == "duplicate_key":
        return "This frontmatter key appears more than once."
    if code == "invalid_type":
        return "This frontmatter field has a YAML value of the wrong type."
    return "This frontmatter key uses YAML the plugin cannot rewrite."


def _identity_fields(kind):
    fields = ["id", "name"]
    if kind == "project":
        fields.append("client")
    return fields


def _missing_field_findings(record, path):
    if "missing_field" not in record.defects:
        return []
    properties = record.properties or {}
    out = []
    for key in _identity_fields(record.kind):
        if key in record.duplicate_keys or key in record.unsupported_keys:
            continue
        if properties.get(key) is None:
            out.append(Finding("error", "missing_field",
                               f"A required frontmatter field is missing: {key}.",
                               path=path, key=key))
    return out


def _identity_key_findings(record, path):
    identity = set(_IDENTITY_KEYS)
    if record.kind == "project":
        identity.add("client")
    out = []
    for key in sorted((record.duplicate_keys | record.unsupported_keys | record.mistyped_keys) & identity):
        if key in record.duplicate_keys:
            code = "duplicate_key"
        elif key in record.unsupported_keys:
            code = "unsupported_yaml"
        else:
            code = "invalid_type"
        if code not in record.defects:
            continue
        out.append(Finding("error", code, _property_message(code),
                           path=path, line=_key_line(record, key), key=key))
    return out


def _duplicate_id_findings(entry, record, path, paths_by_id):
    others = sorted(other for other in paths_by_id.get(entry.id, []) if other != path)
    if not others:
        return []
    return [Finding("error", "duplicate_id", "The id is also used by " + ", ".join(others) + ".",
                    path=path, line=_key_line(record, "id"), key="id")]


def _section_findings(record, data, path):
    out = []
    for section in record.sections:
        line = _line_at(data, section.start)
        if record.section_defects.get(section.key):
            out.append(Finding("warning", "duplicate_section",
                               "More than one heading uses this section key.",
                               path=path, line=line, section=section.key))
        if _CLOSING_HASHES.search(section.title):
            out.append(Finding("warning", "heading_closing_sequence", _HEADING_MESSAGE,
                               path=path, line=line, section=section.key))
    return out


def _entry_findings(entry, record, data, paths_by_id):
    path = entry.relpath
    defects = set(record.global_defects) | set(record.defects)
    out = []
    if "yaml_parse" in defects:
        out.append(Finding("error", "yaml_parse", "The frontmatter is not valid YAML.",
                           path=path, line=1))
    if "encoding" in defects:
        out.append(Finding("error", "encoding", "The record is not valid UTF-8.",
                           path=path, line=1))
    if "mixed_eol" in defects:
        out.append(Finding("error", "mixed_eol", "The record mixes CRLF and LF line endings.",
                           path=path, line=1))
    if record.is_record and record.kind is None:
        out.append(Finding("error", "kind_conflict",
                           "The engagements markers disagree, so the record kind cannot be determined.",
                           path=path, line=1))
    out.extend(_missing_field_findings(record, path))
    if "invalid_id" in defects:
        out.append(Finding("error", "invalid_id",
                           "The id is not a valid id (lowercase letters, digits and hyphens).",
                           path=path, line=_key_line(record, "id"), key="id"))
    out.extend(_identity_key_findings(record, path))
    if "duplicate_id" in entry.defects:
        out.extend(_duplicate_id_findings(entry, record, path, paths_by_id))
    if "unknown_client" in entry.defects:
        out.append(Finding("error", "unknown_client",
                           "The project names a client that is not a client record in this library.",
                           path=path, line=_key_line(record, "client"), key="client"))
    for key, code in sorted(record.property_defects.items()):
        out.append(Finding("warning", code, _property_message(code),
                           path=path, line=_key_line(record, key), key=key))
    out.extend(_section_findings(record, data, path))
    out.extend(_secret_findings(path, data))
    return out


# --- secret-shaped strings -------------------------------------------------------


def _secret_findings(path, data):
    """One finding per detected credential in the record, every one of them.

    The patterns live in ``redact`` so the write path and this report use one
    detector. Besides the record's ``path``, a finding carries only the kind and
    where the value sits (area, section index, line, occurrence on the line); it
    never carries the value, a heading or any other text from the record, so doctor
    can keep reporting a record until the value is gone.
    """
    text = data.decode("utf-8", "replace")
    return [Finding("warning", "secret_shaped",
                    SECRET_FINDING_MESSAGE.format(
                        kind=finding["kind"],
                        location=location_text(finding["area"], finding["section"], finding["line"],
                                               finding["occurrence"])),
                    path=path, line=finding["line"], area=finding["area"],
                    section_index=finding["section"], kind=finding["kind"],
                    occurrence=finding["occurrence"])
            for finding in secret_findings(text)]


# --- locations ------------------------------------------------------------------


@dataclass
class _Place:
    project: str
    project_id: str | None
    line: int | None
    canonical: str | None
    present: bool


def _inside(inner, outer, os_name):
    separator = ntpath.sep if os_name == "nt" else posixpath.sep
    outer = outer.rstrip(separator) or separator
    if inner == outer:
        return False
    prefix = outer if outer.endswith(separator) else outer + separator
    return inner.startswith(prefix)


def _project_places(entry, record, home, os_name, exists, realpath):
    locations = record.properties.get("locations")
    line = _key_line(record, "locations")
    places = []
    for value in locations:
        if not isinstance(value, str):
            continue
        canonical, present = _canonical(value, home, os_name, exists, realpath)
        places.append(_Place(entry.relpath, entry.id, line, canonical, present))
    return places


def _location_findings(active_projects, every_project, os_name):
    """Missing locations for every project; shared/nested ambiguity only for active ones.

    SPEC 6.6 does not limit missing-location reporting to active projects, so a
    historical project whose folder is gone is reported too. The ambiguity checks
    are about auto-binding, which only consults active projects, so they stay
    active-only.
    """
    active = [place for project in active_projects for place in project]
    out = []

    for place in (place for project in every_project for place in project):
        if place.canonical is not None and not place.present:
            out.append(Finding("notice", "missing_location",
                               "A local location does not exist on this host.",
                               path=place.project, line=place.line, key="locations"))

    by_path = {}
    for place in active:
        if place.canonical is not None:
            by_path.setdefault(place.canonical, []).append(place)
    for canonical in sorted(by_path):
        members = _distinct_projects(by_path[canonical])
        if len(members) < 2:
            continue
        for place in members:
            others = sorted(_name(other.project_id, other.project)
                            for other in members if other.project != place.project)
            out.append(Finding("notice", "shared_location",
                               "The location is shared with another active project ("
                               + ", ".join(others) + ").",
                               path=place.project, line=place.line, key="locations"))

    nested = {}
    for place in active:
        if place.canonical is None:
            continue
        containers = sorted({_name(other.project_id, other.project)
                             for other in active
                             if other.project != place.project and other.canonical is not None
                             and _inside(place.canonical, other.canonical, os_name)})
        if containers:
            entry = nested.setdefault(place.project, (place.line, set()))
            entry[1].update(containers)
    for project in sorted(nested):
        line, containers = nested[project]
        out.append(Finding("notice", "nested_location",
                           "The location is inside another active project's location ("
                           + ", ".join(sorted(containers)) + ").",
                           path=project, line=line, key="locations"))
    return out


def _distinct_projects(members):
    ordered = {}
    for place in sorted(members, key=lambda place: place.project):
        ordered.setdefault(place.project, place)
    return list(ordered.values())


# --- the run --------------------------------------------------------------------


def _root_missing():
    return Finding("error", "library_missing",
                   "The library root does not exist or is not a directory.")


def _io_error(path, message="This path could not be read."):
    return Finding("error", "io_error", message, path=path)


def _library_yaml_finding(state):
    if state == "missing":
        message = "The library metadata file .engagements/library.yaml is missing."
    else:
        message = "The library metadata file .engagements/library.yaml is invalid or has the wrong format."
    return Finding("error", "library_yaml", message, path=LIBRARY_YAML)


def _open_error(root, exc):
    return _io_error(_relative(root, exc.details.get("path")), "The library could not be opened.")


def run_doctor(root, *, home=None, os_name=None, exists=None, realpath=None):
    """Diagnose the library at ``root`` without writing anything.

    ``root`` is a path, not an opened ``Library``: the library may fail to open,
    and that failure is itself a finding. ``home``, ``os_name``, ``exists`` and
    ``realpath`` are injectable for deterministic tests and mean what they mean
    in ``engagements.binding.canonicalize``.
    """
    try:
        text = os.fsdecode(os.fspath(root))
        resolved = os.path.realpath(os.path.expanduser(text))
    except (OSError, RuntimeError, TypeError, ValueError):
        return DoctorReport(ROOT_DISPLAY, 0, [_root_missing()])
    if os_name is None:
        os_name = os.name
    if home is None:
        try:
            home = os.path.expanduser("~")
        except (OSError, RuntimeError):
            home = ""
    try:
        is_directory = os.path.isdir(resolved)
    except OSError:
        is_directory = False
    if not is_directory:
        return DoctorReport(ROOT_DISPLAY, 0, [_root_missing()])

    findings = []
    try:
        top, identity = _open_root(Path(resolved))
    except _Refusal as exc:
        return DoctorReport(ROOT_DISPLAY, 0, [_io_error(_relative(resolved, exc.relpath))])
    except OSError as exc:
        return DoctorReport(ROOT_DISPLAY, 0,
                            [_io_error(_relative(resolved, getattr(exc, "filename", None)),
                                       "The library could not be read.")])
    try:
        state, identifier = _metadata_state(top)
    except EngagementsError as exc:
        return DoctorReport(ROOT_DISPLAY, 0, [_open_error(resolved, exc)])
    except _Refusal as exc:
        return DoctorReport(ROOT_DISPLAY, 0, [_io_error(_relative(resolved, exc.relpath))])
    except OSError as exc:
        return DoctorReport(ROOT_DISPLAY, 0,
                            [_io_error(_relative(resolved, getattr(exc, "filename", None)),
                                       "The library could not be read.")])
    finally:
        top.close()

    if state != "valid":
        findings.append(_library_yaml_finding(state))
    # The identity comes from the first opened root for every metadata state, so a
    # root replaced after this point is refused instead of read.
    library = Library(Path(resolved), "" if identifier is None else identifier, False, identity)

    try:
        # Doctor always reads every file: its report is about what the files say,
        # and it never says anything about the scan cache.
        discovery = library.discover(fresh=True)
    except EngagementsError as exc:
        findings.append(_io_error(_relative(resolved, exc.details.get("path")),
                                  "The library could not be read."))
        return DoctorReport(ROOT_DISPLAY, 0, findings)
    except (OSError, RuntimeError, ValueError):
        findings.append(_io_error(None, "The library could not be scanned."))
        return DoctorReport(ROOT_DISPLAY, 0, findings)

    findings.extend(_discovery_findings(resolved, discovery))
    paths_by_id = {}
    for entry in discovery.entries:
        if isinstance(entry.id, str):
            paths_by_id.setdefault(entry.id, []).append(entry.relpath)

    projects = []
    every_project = []
    for entry in discovery.entries:
        try:
            data = _read_bytes(library, entry.relpath)
        except Exception:  # noqa: BLE001 - any read failure is a finding, never a traceback
            findings.append(_io_error(entry.relpath, "This record could not be read."))
            continue
        try:
            record = records_module.parse(data)
        except Exception:  # noqa: BLE001 - a parser failure is reported, not raised
            findings.append(Finding("error", "yaml_parse",
                                    "The frontmatter could not be parsed.",
                                    path=entry.relpath, line=1))
            continue
        findings.extend(_entry_findings(entry, record, data, paths_by_id))
        if _is_location_project(entry, record):
            places = _project_places(entry, record, home, os_name, exists, realpath)
            every_project.append(places)
            if entry.status == "active":
                projects.append(places)

    findings.extend(_location_findings(projects, every_project, os_name))
    return DoctorReport(ROOT_DISPLAY, len(discovery.entries), findings)


def _discovery_findings(root, discovery):
    findings = []
    if discovery.listed > WALK_ENTRY_WARNING:
        findings.append(_large_library_finding(discovery.listed))
    if discovery.incomplete:
        findings.append(Finding("error", "incomplete_scan",
                                "The library root is too large to scan in full: the walk stopped after "
                                f"{WALK_ENTRY_LIMIT:,} directory entries. Everything it did not reach is "
                                "unchecked; point library_root at a narrower folder.",
                                path=ROOT_DISPLAY))
    findings.extend(_io_error(_relative(root, relpath) or relpath)
                    for relpath in discovery.unreadable)
    for relpath in discovery.skipped_symlinks:
        findings.append(Finding("warning", "skipped_symlink",
                                "A symbolic link was skipped; records must be real files inside the library.",
                                path=_relative(root, relpath) or relpath))
    for relpath, _size in discovery.oversize:
        findings.append(Finding("warning", "oversize",
                                "The file is over 1 MiB and was not read.",
                                path=_relative(root, relpath) or relpath))
    findings.extend(_stale_temp_findings(root))
    return findings


def _large_library_finding(listed):
    """The warning for a root big enough that scanning it is the user's to narrow.

    The count is the number of directory entries the walk listed, records or not:
    that is the work a scan does, and a library inside a notes vault pays for the
    notes too. Nothing about the cache is reported.
    """
    return Finding("warning", "large_library",
                   f"The library root is very large: {listed:,} directory entries were listed. "
                   "Point library_root at a narrower folder to scan less.",
                   path=ROOT_DISPLAY)


def _stale_temp_findings(root):
    """Temp files an interrupted write left behind, reported with their age and never removed.

    Only the plugin's own temp names count, and a symlink is not one. Nothing
    deletes these files: an old temp file is not evidence that no writer holds
    it, so removing it could abort a live write (spec 5.1, 6.6).
    """
    try:
        stale = writer.stale_temp_files(root, clock=lambda: datetime.now(UTC))
    except (EngagementsError, OSError, RuntimeError, ValueError):
        return [_io_error(None, "The library could not be scanned for stale temporary files.")]
    return [Finding("warning", "stale_temp",
                    f"A temporary file from an interrupted write is still here, unused for {_age_text(age)}; "
                    "a writer may still hold it, so nothing was removed.",
                    path=relpath)
            for relpath, age in stale]


def _age_text(seconds):
    """A rough age in the largest unit that describes it, for a doctor message."""
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if seconds >= size:
            count = int(seconds // size)
            return f"{count} {unit}" if count == 1 else f"{count} {unit}s"
    return "less than a minute"


def _is_location_project(entry, record):
    """A project whose ``locations`` are readable, active or not."""
    return (entry.kind == "project" and isinstance(record.properties, dict)
            and isinstance(record.properties.get("locations"), list))

