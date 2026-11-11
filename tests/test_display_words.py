"""Every message the plugin renders names the entity the way the profile configured it.

The display words are configuration: a profile may rename ``labels.client`` and
``labels.project`` (Account and Matter in these tests). The rule, stated for readers in
``docs/TOOLS.md``, is that every sentence the model or the user reads uses those words,
while tool names, argument names, ``kind`` values, reason codes, JSON keys, file names
and text quoted from a record keep their own spelling.

The inventory below is taken from the code, not from examples:

- ``test_no_presentation_literal_names_the_entity_beside_its_label`` walks the modules
  that word model-facing text and reports every sentence literal that still carries a
  default word. A new message constant or formatter is caught here as soon as it is
  written, whether or not this file renders it yet.
- ``test_every_lower_layer_message_is_rendered_at_the_tool_boundary`` discovers every
  module in ``engagements/`` rather than a kept list, and requires every sentence in a
  core module that names the entity, by writing the word or by substituting a ``kind``
  value, to be declared as a refusal the tool boundary re-words, or to be a sentence the
  rule leaves alone with its reason written down. A message produced in a lower layer
  cannot slip past the labels by not being mentioned here. The scan reads literals and
  ``kind`` substitutions; an entity word carried in some other variable is outside what it
  can see.
- ``test_every_message_constant_is_exercised`` reads the module-level message constants
  of the presentation modules and requires each to appear in the rendered inventory.
- The rendering tests drive every registered tool twice, once through the public entry
  point and once through the callback Hermes itself calls, and every banner variant,
  ``/client``, ``/engagements`` and the CLI subcommands under Account and Matter; they
  check the text that arrives.

Scope: the strings the plugin itself words from configuration. Two surfaces are outside
the rule and are checked by their own suites instead: the static tool descriptions
(registered once at import, so no setting can reach them, and the rule leaves tool names
alone) and the doctor report, whose notices name record kinds (``another active project``,
``not a client record``) as part of an integrity report about files, not the engagement
entity the profile renames.
"""

import ast
import re
from functools import partial
from pathlib import Path
from typing import NamedTuple

import plugin_support
import pytest
import tools_support
from engagements import binding, runtime, tools
from tools_support import call, copy_library, make_session

LABELS = {"client": "Account", "project": "Matter"}
DEFAULT_LABELS = {"client": "Client", "project": "Project"}
REPO = Path(__file__).resolve().parent.parent
PRESENTATION = ("engagements/binding.py", "engagements/runtime.py", "engagements/tools.py")

# Surfaces that word their own copy, each with the reason the configured labels do not reach
# it. This is not the list of modules that is scanned: the scan below discovers every module
# in the package, and a module that is neither here nor a presentation module is held to the
# boundary rule, so a new core layer is covered the day it is added.
OUTSIDE_THE_RULE = {
    "engagements/doctor.py": "an integrity report about files; its notices name record kinds, not the renamed entity",
    "engagements/dashboard_data.py": "a separate read-only reader; its fields describe the library, not model-facing text",
    "engagements/tool_schemas.py": "the static tool descriptions, registered once at import, so no setting can reach them",
}

# Sentence literals that carry a default word but are not the wording a configured label has
# to reach, each with its module and reason. Keeping this list short and reasoned is the
# point: a new sentence that names the entity has to be rendered, not excused here.
NOT_DISPLAY_TEXT = {
    ("engagements/binding.py", "missing client"): "stored-binding problem code; only its truthiness is read",
    ("engagements/binding.py", "bad project"): "stored-binding problem code; only its truthiness is read",
    ("engagements/tools.py", "Client not found."): "a core layer's default, declared in the table that re-words it",
    ("engagements/records.py", r"(?m)^engagements:[ \t]*(client|project)[ \t]*$"):
        "a parsing pattern matched against file bytes, never shown",
    ("engagements/redact.py", "client[ _-]secret"):
        "a credential keyword pattern matched against record text, never shown",
}


# Core sentences whose ``kind`` hole is record syntax rather than a message to the model or the
# user, so the configured labels must not reach them: record syntax keeps its own spelling when
# it is quoted back. Two are serialized into a record file's bytes; the third is the identity
# line a footprint reports, and is never written. ``(module, sentence with {holes})``.
RECORD_SYNTAX = (
    # The frontmatter line a new record is serialized with.
    ("engagements/records.py", "engagements: {kind}"),
    # Its first change-log line.
    ("engagements/records.py", "- {date} {profile}: created {kind} (source: {source})"),
    # The expected ``engagements`` line of an identity conflict, which the error reports as that
    # footprint's ``base_text``. No repair writes it to a record, so it is not bytes on disk.
    ("engagements/writer.py", "engagements: {kind}"),
)


class _BoundaryRefusal(NamedTuple):
    """One sentence a core layer writes, where it is re-worded, and what that renders.

    ``default`` is the sentence the lower layer writes, with its substitutions as ``{holes}``;
    ``rendered`` is the sentence the inventory must show under Account and Matter, taken from
    the case that reaches that producer; ``boundary`` names the tool boundary function that
    renders the labels into it.
    """

    default: str
    rendered: str
    boundary: str


SIMILAR_EXISTS = "A {kind} named like '{name}' already exists. Use it, or pass allow_similar to create another."

# The producer-to-display contract for lower layers: every sentence that names the entity in
# a core module has to appear here, and its ``rendered`` sentence has to appear in the
# rendered inventory. That binds the static scan to behaviour: declaring a sentence without a
# case that reaches it fails the second test.
BOUNDARY_REFUSALS = (
    _BoundaryRefusal("Client not found.", "Account not found.", "_boundary_error"),
    _BoundaryRefusal("No client with id '{client}'.", "No Account with id 'no-such-record'.", "_create_error"),
    _BoundaryRefusal("Client '{client}' is not active.", "Account 'acme-bakery' is not active.", "_create_warning"),
    _BoundaryRefusal("No client or project matches '{ref}'.", "No Account or Matter matches 'no-such-record'.",
                     "_resolve"),
    # One sentence whose entity word is the ``kind`` it substitutes, declared once per kind so
    # losing either branch of the boundary's rendering fails the inventory.
    _BoundaryRefusal(SIMILAR_EXISTS, "A Account named like 'Acme Bakery' already exists. "
                     "Use it, or pass allow_similar to create another.", "_create_error"),
    _BoundaryRefusal(SIMILAR_EXISTS, "A Matter named like 'Website 2026' already exists. "
                     "Use it, or pass allow_similar to create another.", "_create_error"),
)


def _package_modules():
    """Every module of the plugin, discovered rather than listed."""
    return sorted(path for path in (REPO / "engagements").glob("*.py") if path.name != "__init__.py")


def _literal_pattern(text):
    """A sentence with ``{holes}`` as a pattern that matches it with anything in the holes."""
    parts = re.split(r"\{[^}]*\}", text)
    return re.compile(".+?".join(re.escape(part) for part in parts), re.S)


# --- the rule, as a check on the text ----------------------------------------------------


_TOOL_OR_ARGUMENT = re.compile(r"`[^`]*`")  # kind values, tool names and argument names
_QUOTED_DATA = re.compile(r'"[^"]*"')  # quoted record data in a banner
_QUOTED_REFERENCE = re.compile(r"'[^']*'")  # a quoted id or record name in a message
_COMMAND = re.compile(r"/client\b")  # the command's own identifier
_KIND_ANNOTATION = re.compile(r"\((?:[^(),]+, )?(?:client|project)\)")  # `(id, kind)` in `show`
_PATH_TOKEN = re.compile(r"[^\s,;:()\[\]]*[/\\][^\s,;:()\[\]]*")  # a file or folder name
_ENTITY_WORD = re.compile(r"(?<![\w])(?:client|project)s?'?(?![\w])", re.I)


def bare_entity_words(text):
    """The words in ``text`` that name the entity without using the configured label."""
    stripped = _TOOL_OR_ARGUMENT.sub(" ", text)
    stripped = _QUOTED_DATA.sub(" ", stripped)
    stripped = _QUOTED_REFERENCE.sub(" ", stripped)
    stripped = _COMMAND.sub(" ", stripped)
    stripped = _KIND_ANNOTATION.sub(" ", stripped)
    stripped = _PATH_TOKEN.sub(" ", stripped)
    return [match.group(0) for match in _ENTITY_WORD.finditer(stripped)]


def offenders(texts):
    """``{text: words}`` for every rendered string that names the entity itself."""
    found = {}
    for text in texts:
        words = bare_entity_words(text)
        if words:
            found[text] = words
    return found


# --- the first half: every sentence literal in the presentation modules -------------------


def _docstring_nodes(tree):
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                found.add(id(body[0].value))
    return found


KIND_HOLE = "{kind}"


def _is_kind(expression):
    """Whether a substitution writes a record's ``kind``: the bare word ``client`` or ``project``."""
    if isinstance(expression, ast.Name):
        return expression.id == "kind"
    return isinstance(expression, ast.Attribute) and expression.attr == "kind"


def _hole(value, kind_holes):
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return value.value
    if kind_holes and isinstance(value, ast.FormattedValue) and _is_kind(value.value):
        return KIND_HOLE
    return " "


def _sentence_literals(relative, *, kind_holes=False):
    """``(line, text)`` for every non-docstring literal that is a sentence.

    An f-string is measured as one text with its substitutions removed, so a message that
    writes the entity word outside a placeholder is seen here exactly as the model sees it.
    Its parts are not measured again on their own.

    With ``kind_holes`` a substitution of a ``kind`` value is kept as ``{kind}`` instead of
    being removed. A core layer cannot see the labels, so ``kind`` is the other way it can put
    the entity word into a sentence (``A {kind} named like ...`` renders ``A client ...``);
    erasing it would hide that sentence from the inventory.
    """
    source = (REPO / relative).read_text(encoding="utf-8")
    tree = ast.parse(source)
    docstrings = _docstring_nodes(tree)
    nested = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            nested.update(id(child) for child in ast.walk(node) if child is not node)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docstrings or id(node) in nested:
                continue
            text = node.value
        elif isinstance(node, ast.JoinedStr):
            text = "".join(_hole(value, kind_holes) for value in node.values)
        else:
            continue
        if len(text.replace("_", " ").split()) > 1:  # a sentence, not a code or a key
            found.append((node.lineno, text))
    return found


def test_no_presentation_literal_names_the_entity_beside_its_label():
    failures = []
    for relative in PRESENTATION:
        for line, text in _sentence_literals(relative):
            words = bare_entity_words(text)
            if not words or (relative, text.strip()) in NOT_DISPLAY_TEXT:
                continue
            failures.append(f"{relative}:{line}: {text!r} names {words}")
    assert failures == []


# --- the same rule, for the messages a lower layer produces -------------------------------


def _core_sentence_literals():
    """``(module, line, text)`` for every entity-naming sentence a core module writes.

    The modules are discovered, not listed: presentation modules word their own text from the
    labels and are covered above, the surfaces in ``OUTSIDE_THE_RULE`` are accounted for by
    their own suites, and everything else is a core layer whose sentences have to be re-worded
    at the tool boundary. A sentence names the entity when it writes the word itself or
    substitutes a ``kind`` value into running text.
    """
    for path in _package_modules():
        relative = f"engagements/{path.name}"
        if relative in PRESENTATION or relative in OUTSIDE_THE_RULE:
            continue
        for line, text in _sentence_literals(relative, kind_holes=True):
            if _names_the_entity(text) and not _excused(relative, text.strip()):
                yield relative, line, text.strip()


def _names_the_entity(text):
    return bool(bare_entity_words(text)) or KIND_HOLE in text


def _excused(relative, text):
    """Whether a core sentence is one ``NOT_DISPLAY_TEXT`` or ``RECORD_SYNTAX`` accounts for."""
    if (relative, text) in NOT_DISPLAY_TEXT:
        return True
    return any(module == relative and _literal_pattern(template).fullmatch(text)
               for module, template in RECORD_SYNTAX)


def test_every_lower_layer_message_is_rendered_at_the_tool_boundary():
    unmatched, seen = [], set()
    for relative, line, text in _core_sentence_literals():
        indices = [i for i, spec in enumerate(BOUNDARY_REFUSALS) if _literal_pattern(spec.default).fullmatch(text)]
        if not indices:
            named = bare_entity_words(text) + ([KIND_HOLE] if KIND_HOLE in text else [])
            unmatched.append(f"{relative}:{line}: {text!r} names {named} and is not "
                             "re-worded at the tool boundary")
            continue
        seen.update(indices)
        for index in indices:
            assert callable(getattr(tools, BOUNDARY_REFUSALS[index].boundary, None)), \
                f"{relative}:{line}: no boundary function {BOUNDARY_REFUSALS[index].boundary}"
    stale = [spec.default for index, spec in enumerate(BOUNDARY_REFUSALS) if index not in seen]
    assert unmatched == []
    assert stale == [], "declared boundary refusals no core module writes any more"


# --- the tool messages -------------------------------------------------------------------


class _Case:
    """One case's fixture: its library copy, the state store it can tamper with, and the way
    it reaches a tool.

    ``through`` selects the path the case takes. ``dispatcher`` builds a ``tools.Session`` and
    calls the public entry point; ``registered`` registers the plugin on a fake host context
    and calls the callback Hermes itself calls, so the labels are read from settings the way a
    real profile supplies them. Both use the same session id and the same state store shape,
    so a case body is the same either way.
    """

    def __init__(self, tmp_path, name, *, through="dispatcher", monkeypatch=None, labels=LABELS,
                 cwd=None, session_id="sess-1", library=True):
        self.name = name
        self.session_id = session_id
        self.through = through
        self._session = self._ctx = None
        self.root = copy_library(Path(tmp_path) / name) if library else None
        if through == "registered":
            _, ctx = plugin_support.registered(monkeypatch, Path(tmp_path) / name, cwd=cwd or "",
                                               library=self.root, settings={"labels": dict(labels)})
            self._ctx = ctx
            self.store = ctx.state.values
        else:
            self._session, self.store = make_session(self.root, labels=labels, cwd=cwd,
                                                     session_id=session_id)

    def __call__(self, tool, args):
        if self._ctx is not None:
            return self._ctx.call_tool(tool, args, session_id=self.session_id)
        return call(tool, args, self._session)

    def tamper(self, key, value):
        """Hold a stored value the way a corrupt or hand-edited host store would."""
        self.store[key] = value


def _open_case(tmp_path, name, *, through, monkeypatch, **kwargs):
    return _Case(tmp_path, name, through=through, monkeypatch=monkeypatch, **kwargs)


def _tool_strings(result):
    """Every human-readable string one tool envelope carries."""
    out = []
    if result.get("ok"):
        data = result.get("data") or {}
        out.append(str(data.get("evidence") or ""))
        out.append(str((data.get("auto") or {}).get("evidence") or ""))
    else:
        error = result.get("error") or {}
        details = error.get("details") or {}
        out.append(str(error.get("message") or ""))
        out.append(str(details.get("evidence") or ""))
        out.append(str((details.get("auto") or {}).get("evidence") or ""))
    out.extend(str(warning.get("message") or "") for warning in result.get("warnings") or ())
    return [text for text in out if text]


def _deactivate(path):
    path.write_bytes(path.read_bytes().replace(b"status: active", b"status: former", 1))


def case_find_all(tmp_path, open_case):
    case = open_case(tmp_path, "find-all")
    return [case("engagements_find", {"query": ""})]


def case_find_no_match(tmp_path, open_case):
    case = open_case(tmp_path, "find-no-match")
    return [case("engagements_find", {"query": "nothing like this"})]


def case_context_unknown_id(tmp_path, open_case):
    case = open_case(tmp_path, "context-unknown-id")
    return [case("engagements_context", {"id": "no-such-record"})]


def case_context_wrong_kind(tmp_path, open_case):
    case = open_case(tmp_path, "context-wrong-kind")
    return [case("engagements_context", {"id": "acme-bakery", "kind": "project"})]


def case_context_unbound(tmp_path, open_case):
    case = open_case(tmp_path, "context-unbound")
    return [case("engagements_context", {})]


def case_context_unbound_in_a_folder(tmp_path, open_case):
    case = open_case(tmp_path, "context-unbound-folder", cwd="/work/nothing/here")
    return [case("engagements_context", {})]


def case_context_bound_client(tmp_path, open_case):
    case = open_case(tmp_path, "context-bound-client")
    return [case("engagements_bind", {"client": "acme-bakery"}),
            case("engagements_context", {})]


def case_context_bound_project(tmp_path, open_case):
    case = open_case(tmp_path, "context-bound-project")
    return [case("engagements_bind", {"client": "acme-bakery", "project": "acme-website-2026"}),
            case("engagements_context", {"kind": "project"}),
            case("engagements_context", {})]


def case_context_invalid_binding(tmp_path, open_case):
    case = open_case(tmp_path, "context-invalid")
    case("engagements_bind", {"client": "acme-bakery"})
    _deactivate(case.root / "Acme Bakery" / "Client.md")
    return [case("engagements_context", {})]


def case_context_stored_unreadable(tmp_path, open_case):
    case = open_case(tmp_path, "context-stored-unreadable")
    case.tamper(tools_support.binding_key_for(case.root, case.session_id), ["not a binding"])
    return [case("engagements_context", {})]


def case_context_no_session(tmp_path, open_case):
    case = open_case(tmp_path, "context-no-session", session_id=None)
    return [case("engagements_context", {})]


def case_context_multiline_reference(tmp_path, open_case):
    case = open_case(tmp_path, "context-multiline")
    return [case("engagements_context", {"id": "acme\nbakery"})]


def case_bind_unknown_client(tmp_path, open_case):
    case = open_case(tmp_path, "bind-unknown-client")
    return [case("engagements_bind", {"client": "no-such-record"})]


def case_bind_unknown_project(tmp_path, open_case):
    case = open_case(tmp_path, "bind-unknown-project")
    return [case("engagements_bind", {"project": "no-such-record"})]


def case_bind_owner_mismatch(tmp_path, open_case):
    case = open_case(tmp_path, "bind-mismatch")
    return [case("engagements_bind", {"client": "acme-bakery", "project": "riverbend-recall-campaign"})]


def case_bind_off_and_auto(tmp_path, open_case):
    case = open_case(tmp_path, "bind-modes")
    case("engagements_bind", {"client": "acme-bakery"})
    return [case("engagements_bind", {"mode": "off"}),
            case("engagements_bind", {"mode": "auto"})]


def case_bind_project_only(tmp_path, open_case):
    case = open_case(tmp_path, "bind-project-only")
    return [case("engagements_bind", {"project": "acme-website-2026"})]


def case_bind_no_session(tmp_path, open_case):
    case = open_case(tmp_path, "bind-no-session", session_id=None)
    return [case("engagements_bind", {"client": "acme-bakery"})]


def case_bind_inactive_record(tmp_path, open_case):
    case = open_case(tmp_path, "bind-inactive")
    _deactivate(case.root / "Acme Bakery" / "Client.md")
    return [case("engagements_bind", {"client": "acme-bakery"})]


def case_create_unknown_owner(tmp_path, open_case):
    case = open_case(tmp_path, "create-unknown-owner")
    return [case("engagements_create", {"kind": "project", "name": "Synthetic audit",
                                        "client": "no-such-record"})]


def case_create_similar_name(tmp_path, open_case):
    case = open_case(tmp_path, "create-similar")
    return [case("engagements_create", {"kind": "project", "name": "Website 2026",
                                        "client": "acme-bakery"})]


def case_create_similar_client(tmp_path, open_case):
    case = open_case(tmp_path, "create-similar-client")
    return [case("engagements_create", {"kind": "client", "name": "Acme Bakery"})]


def case_create_under_an_inactive_owner(tmp_path, open_case):
    case = open_case(tmp_path, "create-inactive-owner")
    _deactivate(case.root / "Acme Bakery" / "Client.md")
    return [case("engagements_create", {"kind": "project", "name": "Synthetic audit",
                                        "client": "acme-bakery"})]


def case_create_client_and_project(tmp_path, open_case):
    case = open_case(tmp_path, "create-records")
    return [case("engagements_create", {"kind": "client", "name": "Synthetic Client"}),
            case("engagements_create", {"kind": "project", "name": "Synthetic Project",
                                        "client": "acme-bakery"})]


def case_update_unknown_id(tmp_path, open_case):
    case = open_case(tmp_path, "update-unknown")
    return [case("engagements_update", {"id": "no-such-record", "base_rev": "0123456789abcdef",
                                        "source": "synthetic probe",
                                        "ops": [{"op": "set_property", "key": "status", "value": "active"}]})]


def case_update_record(tmp_path, open_case):
    case = open_case(tmp_path, "update-record")
    read = case("engagements_context", {"id": "acme-bakery"})
    return [case("engagements_update", {"id": "acme-bakery", "base_rev": read["data"]["rev"],
                                        "source": "synthetic probe",
                                        "ops": [{"op": "set_property", "key": "status", "value": "active"}]})]


def case_update_missing_owner(tmp_path, open_case):
    """A valid project whose new owner is absent: the refusal comes from the records layer."""
    case = open_case(tmp_path, "update-missing-owner")
    read = case("engagements_context", {"id": "acme-website-2026"})
    return [case("engagements_update", {"id": "acme-website-2026", "base_rev": read["data"]["rev"],
                                        "source": "synthetic audit",
                                        "ops": [{"op": "set_property", "key": "client",
                                                 "value": "no-such-record"}]})]


def case_missing_library(tmp_path, open_case):
    case = open_case(tmp_path, "missing-library", library=False)
    return [case("engagements_context", {})]


def case_unknown_tool(tmp_path, open_case):
    case = open_case(tmp_path, "unknown-tool")
    return [case("engagements_not_a_tool", {})]


TOOL_CASES = {
    "find-all": case_find_all,
    "find-no-match": case_find_no_match,
    "context-unknown-id": case_context_unknown_id,
    "context-wrong-kind": case_context_wrong_kind,
    "context-unbound": case_context_unbound,
    "context-unbound-in-a-folder": case_context_unbound_in_a_folder,
    "context-bound-client": case_context_bound_client,
    "context-bound-project": case_context_bound_project,
    "context-invalid-binding": case_context_invalid_binding,
    "context-stored-unreadable": case_context_stored_unreadable,
    "context-no-session": case_context_no_session,
    "context-multiline-reference": case_context_multiline_reference,
    "bind-unknown-client": case_bind_unknown_client,
    "bind-unknown-project": case_bind_unknown_project,
    "bind-owner-mismatch": case_bind_owner_mismatch,
    "bind-off-and-auto": case_bind_off_and_auto,
    "bind-project-only": case_bind_project_only,
    "bind-no-session": case_bind_no_session,
    "bind-inactive-record": case_bind_inactive_record,
    "create-unknown-owner": case_create_unknown_owner,
    "create-similar-name": case_create_similar_name,
    "create-similar-client": case_create_similar_client,
    "create-under-an-inactive-owner": case_create_under_an_inactive_owner,
    "create-client-and-project": case_create_client_and_project,
    "update-unknown-id": case_update_unknown_id,
    "update-record": case_update_record,
    "update-missing-owner": case_update_missing_owner,
    "missing-library": case_missing_library,
    "unknown-tool": case_unknown_tool,
}

# Cases the public entry point refuses before dispatch, so there is no registered callback to
# drive: the name is not one Hermes registered.
NOT_REGISTERED = {"unknown-tool": "the entry point refuses a tool name no callback was registered for"}


def test_the_inventory_covers_every_registered_tool():
    exercised = {f"engagements_{name.split('-')[0]}" for name in TOOL_CASES}
    assert set(tools.HANDLERS) <= exercised


@pytest.mark.parametrize("through", ["dispatcher", "registered"])
@pytest.mark.parametrize("case", sorted(TOOL_CASES))
def test_every_tool_message_uses_the_configured_labels(case, through, monkeypatch, tmp_path):
    if through == "registered" and case in NOT_REGISTERED:
        pytest.skip(NOT_REGISTERED[case])
    texts = []
    for result in TOOL_CASES[case](tmp_path, partial(_open_case, through=through, monkeypatch=monkeypatch)):
        texts.extend(_tool_strings(result))
    assert offenders(texts) == {}


@pytest.mark.parametrize("through", ["dispatcher", "registered"])
@pytest.mark.parametrize("labels, expected", [(LABELS, "Account not found."),
                                              (DEFAULT_LABELS, "Client not found.")],
                         ids=["configured-labels", "default-labels"])
def test_a_missing_replacement_owner_is_worded_from_the_labels(through, labels, expected, monkeypatch, tmp_path):
    """An update naming an absent owner is refused in the profile's words, and nothing is written.

    The refusal is produced in the records layer, so this is the producer a scan of the
    presentation modules alone cannot see; the codes and the record's bytes stay as they were.
    """
    case = _Case(tmp_path, f"owner-labels-{through}", through=through, monkeypatch=monkeypatch, labels=labels)
    read = case("engagements_context", {"id": "acme-website-2026"})
    assert read["ok"], read
    record = case.root / read["data"]["path"]
    original = record.read_bytes()
    result = case("engagements_update", {"id": "acme-website-2026", "base_rev": read["data"]["rev"],
                                         "source": "synthetic audit",
                                         "ops": [{"op": "set_property", "key": "client",
                                                  "value": "no-such-record"}]})
    assert not result["ok"]
    assert result["error"]["code"] == "not_found"
    assert result["error"]["details"] == {"reason": "no_match"}
    assert record.read_bytes() == original
    assert result["error"]["message"] == expected


_SIMILAR_ARGS = {
    "client": {"kind": "client", "name": "Acme Bakery"},
    "project": {"kind": "project", "name": "Website 2026", "client": "acme-bakery"},
}


@pytest.mark.parametrize("through", ["dispatcher", "registered"])
@pytest.mark.parametrize("kind", sorted(_SIMILAR_ARGS))
@pytest.mark.parametrize("labels", [LABELS, DEFAULT_LABELS], ids=["configured-labels", "default-labels"])
def test_a_similar_name_refusal_names_the_kind_in_the_profiles_words(labels, kind, through, monkeypatch, tmp_path):
    """The library words this refusal with the ``kind`` it was given, so each kind is its own branch.

    Under Account and Matter each kind renders its own label; under the default labels the
    sentence is exactly the one the library writes. The code, the candidates and the library's
    records stay as they were.
    """
    case = _Case(tmp_path, f"similar-{kind}-{through}", through=through, monkeypatch=monkeypatch, labels=labels)
    root = case.root
    assert root is not None

    def records():
        return {path.relative_to(root): path.read_bytes() for path in root.rglob("*.md")}

    before = records()
    args = _SIMILAR_ARGS[kind]
    result = case("engagements_create", args)
    assert not result["ok"]
    assert result["error"]["code"] == "similar_exists"
    assert result["error"]["details"]["candidates"]
    word = labels[kind] if labels is LABELS else kind
    assert result["error"]["message"] == SIMILAR_EXISTS.format(kind=word, name=args["name"])
    assert records() == before


def test_a_record_that_vanished_under_the_write_keeps_its_own_refusal():
    """The writer's other ``not_found``/``no_match`` refusal is about a file, not the entity.

    It carries a path and its own sentence, so the boundary must leave it exactly as the writer
    raised it rather than render the owner refusal over it. A refusal with the same code and
    reason but another sentence is left alone for the same reason: only the declared default is
    re-worded.
    """
    from engagements.errors import EngagementsError

    vanished = EngagementsError("not_found", "No record at that path.",
                                {"reason": "no_match", "path": "Acme Bakery/Projects/Website 2026.md"})
    assert tools._boundary_error(vanished, LABELS) is vanished
    other = EngagementsError("not_found", "No record with that reference.", {"reason": "no_match"})
    assert tools._boundary_error(other, LABELS) is other


# --- the banner and its notices -----------------------------------------------------------

def banner_for_an_unavailable_library():
    """The one notice the plugin renders with nothing to resolve (the library is unreadable)."""
    return binding.unavailable_text(LABELS)


BANNER_CASES = {
    "unavailable-library": lambda: [banner_for_an_unavailable_library()],
}


@pytest.mark.parametrize("case", sorted(BANNER_CASES))
def test_every_resolved_banner_uses_the_configured_labels(case):
    texts = [text for text in BANNER_CASES[case]() if text]
    assert texts
    assert offenders(texts) == {}


def test_a_quoted_record_name_keeps_its_own_words():
    """A record may be called anything: quoted record data is not the plugin's wording."""
    state = binding.BindingState(mode="explicit", state="bound", method="explicit", client_id="portal-migration",
                                 client_name="Client Portal", project_id=None, evidence="explicit 14:02")
    text = binding.banner_text(state, None, LABELS)
    assert '"Client Portal"' in text
    assert bare_entity_words(text) == []


# --- /client, /engagements and the CLI ----------------------------------------------------


def _plugin(monkeypatch, tmp_path, name, *, labels=LABELS, library=True, **kwargs):
    root = copy_library(tmp_path / name) if library else None
    plugin, ctx = plugin_support.registered(monkeypatch, tmp_path / name, library=root,
                                            settings={"labels": dict(labels)}, **kwargs)
    return ctx, root


def _turn(ctx, session_id="s1"):
    result = ctx.hook("pre_llm_call", session_id=session_id, user_message="hi", conversation_history=[],
                      is_first_turn=False, model="stub", platform=ctx.surface)
    return [result.get("context") or ""] if result else []


def case_status_unbound(monkeypatch, tmp_path):
    ctx, _ = _plugin(monkeypatch, tmp_path, "status-unbound")
    return [ctx.command("client", "", session_id="s1", session_key="key", platform="local")]


def case_status_bound(monkeypatch, tmp_path):
    ctx, _ = _plugin(monkeypatch, tmp_path, "status-bound")
    ctx.call_tool("engagements_bind", {"client": "acme-bakery"}, session_id="s1")
    return [ctx.command("client", "", session_id="s1", session_key="key", platform="local")]


def case_status_ambiguous_folder(monkeypatch, tmp_path):
    home = plugin_support.project_home(monkeypatch, tmp_path / "status-ambiguous")
    ctx, _ = _plugin(monkeypatch, tmp_path, "status-ambiguous", cwd=str(home / "clients" / "shared"))
    return [ctx.command("client", "", session_id="s1", session_key="key", platform="local")]


def case_status_without_the_working_directory(monkeypatch, tmp_path):
    ctx, _ = _plugin(monkeypatch, tmp_path, "status-read-only", surface="telegram")
    return [ctx.command("client", "", session_id="s1", session_key="key", platform="telegram")]


def case_client_list(monkeypatch, tmp_path):
    ctx, _ = _plugin(monkeypatch, tmp_path, "client-list")
    return [ctx.command("client", "list", session_id="s1", session_key="key", platform="local")]


def case_client_bind_and_unbind(monkeypatch, tmp_path):
    ctx, _ = _plugin(monkeypatch, tmp_path, "client-bind")
    return [ctx.command("client", "acme-bakery", session_id="s1", session_key="key", platform="local"),
            ctx.command("client", "off", session_id="s1", session_key="key", platform="local"),
            ctx.command("client", "auto", session_id="s1", session_key="key", platform="local")]


def case_client_unknown_reference(monkeypatch, tmp_path):
    ctx, _ = _plugin(monkeypatch, tmp_path, "client-unknown")
    return [ctx.command("client", "no-such-record", session_id="s1", session_key="key", platform="local")]


def case_client_without_a_session(monkeypatch, tmp_path):
    ctx, _ = _plugin(monkeypatch, tmp_path, "client-no-session")
    return [ctx.command("client", "", session_id=None, session_key="key", platform="local")]


def case_missing_library_commands(monkeypatch, tmp_path):
    """Both missing-library sentences: the default location and a configured one."""
    default_ctx, _ = _plugin(monkeypatch, tmp_path, "missing-default", library=False)
    configured_ctx, _ = _plugin(monkeypatch, tmp_path, "missing-configured")
    (tmp_path / "missing-configured" / "library").rename(tmp_path / "missing-configured" / "moved-away")
    return [default_ctx.command("client", "list", session_id="s1", session_key="key", platform="local"),
            configured_ctx.command("client", "list", session_id="s1", session_key="key", platform="local")]


def case_no_library_location(monkeypatch, tmp_path):
    plugin, ctx = plugin_support.registered(monkeypatch, tmp_path / "no-location",
                                            settings={"labels": dict(LABELS), "library_root": "relative/library"})
    return [ctx.command("client", "list", session_id="s1", session_key="key", platform="local")]


def case_engagements_usage(monkeypatch, tmp_path):
    ctx, _ = _plugin(monkeypatch, tmp_path, "engagements-usage")
    return [ctx.command("engagements", "something", session_id="s1")]


def case_banner_states(monkeypatch, tmp_path):
    """The bound banner, then a changed binding, then the retirement notice."""
    ctx, _ = _plugin(monkeypatch, tmp_path, "banner-states")
    ctx.call_tool("engagements_bind", {"client": "acme-bakery"}, session_id="s1")
    texts = _turn(ctx)
    ctx.call_tool("engagements_bind", {"client": "riverbend-dental"}, session_id="s1")
    texts += _turn(ctx)
    ctx.call_tool("engagements_bind", {"mode": "off"}, session_id="s1")
    texts += _turn(ctx)
    return texts


def case_banner_ambiguous(monkeypatch, tmp_path):
    home = plugin_support.project_home(monkeypatch, tmp_path / "banner-ambiguous")
    ctx, _ = _plugin(monkeypatch, tmp_path, "banner-ambiguous", cwd=str(home / "clients" / "shared"))
    return _turn(ctx)


def case_banner_invalid_binding(monkeypatch, tmp_path):
    ctx, root = _plugin(monkeypatch, tmp_path, "banner-invalid")
    ctx.call_tool("engagements_bind", {"client": "acme-bakery"}, session_id="s1")
    _turn(ctx)
    _deactivate(root / "Acme Bakery" / "Client.md")
    return _turn(ctx)


def case_banner_unavailable_library(monkeypatch, tmp_path):
    ctx, root = _plugin(monkeypatch, tmp_path, "banner-unavailable")
    ctx.call_tool("engagements_bind", {"client": "acme-bakery"}, session_id="s1")
    _turn(ctx)
    (root / ".engagements" / "library.yaml").unlink()
    return _turn(ctx)


def case_banner_incomplete_discovery(monkeypatch, tmp_path):
    """One folder match beside a record whose locations cannot be read: no binding is made."""
    home = plugin_support.project_home(monkeypatch, tmp_path / "banner-incomplete")
    ctx, root = _plugin(monkeypatch, tmp_path, "banner-incomplete",
                        cwd=str(home / "clients" / "acme" / "website-2026"))
    broken = root / "Broken Bakery" / "Projects" / "Bad.md"
    broken.parent.mkdir(parents=True)
    broken.write_text("---\nengagements: project\nid: broken-bakery\nclient: broken-bakery\nname: Bad\n"
                      "status: active\nlocations: not-a-list\n---\n\n## Purpose\nPlaceholder.\n", encoding="utf-8")
    return _turn(ctx)


def case_cli(monkeypatch, tmp_path):
    """``hermes engagements list`` and the two ``show`` refusals, which carry the messages.

    ``show <id>`` also prints the record's own body, which is record text and not the
    plugin's wording, so it is exercised elsewhere rather than scanned here.
    """
    import argparse

    ctx, _ = _plugin(monkeypatch, tmp_path, "cli")
    entry = ctx.cli["engagements"]
    root = argparse.ArgumentParser(prog="hermes")
    sub = root.add_subparsers(dest="command")
    entry["setup_fn"](sub.add_parser("engagements", help=entry["help"]))
    for argv in (["list"], ["show", "no-such-record"], ["show", ""]):
        entry["handler_fn"](root.parse_args(["engagements", *argv]))


def test_the_cli_messages_use_the_configured_labels(monkeypatch, tmp_path, capsys):
    case_cli(monkeypatch, tmp_path)
    out = capsys.readouterr().out
    assert out
    assert offenders(out.splitlines()) == {}
COMMAND_CASES = {
    "status-unbound": case_status_unbound,
    "status-bound": case_status_bound,
    "status-ambiguous-folder": case_status_ambiguous_folder,
    "status-without-the-working-directory": case_status_without_the_working_directory,
    "client-list": case_client_list,
    "client-bind-and-unbind": case_client_bind_and_unbind,
    "client-unknown-reference": case_client_unknown_reference,
    "client-without-a-session": case_client_without_a_session,
    "missing-library-commands": case_missing_library_commands,
    "no-library-location": case_no_library_location,
    "engagements-usage": case_engagements_usage,
    "banner-states": case_banner_states,
    "banner-ambiguous": case_banner_ambiguous,
    "banner-invalid-binding": case_banner_invalid_binding,
    "banner-unavailable-library": case_banner_unavailable_library,
    "banner-incomplete-discovery": case_banner_incomplete_discovery,
}


@pytest.mark.parametrize("case", sorted(COMMAND_CASES))
def test_every_command_and_banner_uses_the_configured_labels(case, monkeypatch, tmp_path):
    texts = [text for text in COMMAND_CASES[case](monkeypatch, tmp_path) if text]
    assert texts
    assert offenders(texts) == {}


# --- every message constant is exercised -------------------------------------------------


def _message_constants():
    """Module-level sentence constants: the plugin's own sentences, by name."""
    constants = {}
    for module in (binding, runtime, tools):
        for name, value in vars(module).items():
            if name.startswith("_") and name.isupper() and isinstance(value, str) and value.strip() \
                    and len(value.split()) > 1:
                constants[f"{module.__name__}.{name}"] = value
    return constants


def _template(value):
    parts = re.split(r"\{\w+\}", value)
    return re.compile(".+?".join(re.escape(part) for part in parts), re.S)


def _inventory_texts(monkeypatch, tmp_path, *, through="dispatcher", labels=LABELS):
    """Every string the plugin shows for the tool, banner and command cases."""
    texts = []
    open_case = partial(_open_case, through=through, monkeypatch=monkeypatch, labels=labels)
    for name, case in TOOL_CASES.items():
        if through == "registered" and name in NOT_REGISTERED:
            continue
        for result in case(tmp_path / f"constant-{name}", open_case):
            texts.extend(_tool_strings(result))
    for case in BANNER_CASES.values():
        texts.extend(case())
    for name, case in COMMAND_CASES.items():
        texts.extend(case(monkeypatch, tmp_path / f"constant-{name}"))
    return texts


def test_every_message_constant_is_exercised(monkeypatch, tmp_path):
    texts = _inventory_texts(monkeypatch, tmp_path)
    not_rendered = {
        "_FALLBACK": "the envelope replaces an unexpected internal failure; it names no entity",
    }
    missing = []
    for name, value in _message_constants().items():
        if name.rsplit(".", 1)[1] in not_rendered:
            continue
        pattern = _template(value)
        if not any(pattern.search(text) for text in texts):
            missing.append(f"{name}: {value!r}")
    assert missing == []


# --- every declared lower-layer refusal is shown, from the producer to the display ---------


def test_every_declared_lower_layer_refusal_is_shown_with_the_labels(monkeypatch, tmp_path):
    """Each declared producer reaches a rendered string, through both dispatch paths."""
    for through in ("dispatcher", "registered"):
        texts = _inventory_texts(monkeypatch, tmp_path / through, through=through)
        for spec in BOUNDARY_REFUSALS:
            assert spec.rendered in texts, f"{spec.rendered!r} is not rendered through the {through} path"


def test_every_declared_lower_layer_refusal_keeps_its_default_sentence(monkeypatch, tmp_path):
    """With default labels the same producers render exactly the sentence they always did."""
    texts = _inventory_texts(monkeypatch, tmp_path / "defaults", labels=DEFAULT_LABELS)
    missing = [spec.default for spec in BOUNDARY_REFUSALS
               if not any(_literal_pattern(spec.default).fullmatch(text) for text in texts)]
    assert missing == []
