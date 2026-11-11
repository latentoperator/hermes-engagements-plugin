"""Golden-case harness for engagements/tools.py (format: tests/golden/README.md).

A case is one JSON file under ``tests/golden/<tool>/``: a fixture copy, an
ordered setup, a session, a request and the complete expected envelope. This
module owns everything mechanical -- fixture copies, the temp home, setup ops,
placeholder substitution, the call and the comparison -- so a case file only
states data.

Nothing here imports ``engagements.tools`` at import time: the tool module and
the session dataclasses are imported inside :func:`run_case`, so collection
works while ``engagements/tools.py`` does not exist yet and every case fails
with ImportError until it does.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

import yaml

TESTS = Path(__file__).resolve().parent
FIXTURES = TESTS / "fixtures"
GOLDEN = TESTS / "golden"

KNOWN_TOOLS = (
    "engagements_find",
    "engagements_bind",
    "engagements_context",
    "engagements_create",
    "engagements_update",
)

TOP_LEVEL_KEYS = frozenset({
    "description",
    "fixture",
    "setup",
    "clock",
    "profile",
    "session",
    "config",
    "request",
    "expected",
    "state_after",
    "files_after",
})

REQUIRED_KEYS = ("fixture", "clock", "session", "request", "expected")

SESSION_KEYS = frozenset({"id", "cwd", "surface", "binding"})

CONFIG_KEYS = frozenset({"auto_bind", "banner", "labels", "library_root", "create_default"})

SETUP_OPS = frozenset({"write", "append", "replace", "delete", "mkdir", "symlink", "snapshot"})

DEFAULT_LABELS = {"client": "Client", "project": "Project"}

_PLACEHOLDER = re.compile(r"<(?:(rev|rev_after|fixture_rev):([^<>]+)|(root|home|message))>")
_ANY_PLACEHOLDER = re.compile(r"<[^<>\n]*>")
_ABSOLUTE = re.compile(r"(?:^|[\s'\"(\[=])(/[^\s'\"()\[\],;]+)")
_REV = re.compile(r"[0-9a-f]{16}")


# --- case discovery ---------------------------------------------------------------


def case_files():
    """``[(tool, path)]`` for every ``tests/golden/<tool>/*.json``, sorted.

    A directory whose name is not a known tool yields its cases too, so the
    parametrized test fails loudly on them instead of hiding them.
    """
    if not GOLDEN.is_dir():
        return []
    found = []
    for directory in sorted(GOLDEN.iterdir()):
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.json")):
            found.append((directory.name, path))
    return found


def load_case(path):
    with open(path, encoding="utf-8") as handle:
        case = json.load(handle)
    if not isinstance(case, dict):
        raise AssertionError(f"{path.name}: a case must be a JSON object")
    return case


# --- the case run -----------------------------------------------------------------


def run_case(tool, path):
    """Run one case against the real handler and assert everything it states."""
    import engagements.binding as binding
    import engagements.tools as tools
    import engagements.writer as writer

    case = load_case(path)
    setup = case.get("setup") or []
    session_spec = case["session"]

    scratch = _temp_dir()
    try:
        root = _copy_fixture(case["fixture"], scratch)
        home = _make_home(scratch)
        clock = _parse_clock(case["clock"])

        with _home(home):
            _apply_setup(root, home, setup, clock, writer)
            before = _record_bytes(root)

            sandbox = _Sandbox(root, home, case["fixture"])
            request = sandbox.many(case["request"])
            expected = sandbox.many(case["expected"])
            state_after = sandbox.optional(case, "state_after")
            files_after = sandbox.optional(case, "files_after")

            state = {}
            key = None
            if session_spec.get("id") is not None:
                library_id = _library_id(root)
                if library_id is not None:
                    key = binding.binding_key(library_id, session_spec["id"])
                elif session_spec.get("binding") is not None:
                    raise AssertionError("the fixture has no library id to store a binding under")
                if key is not None and session_spec.get("binding") is not None:
                    state[key] = session_spec["binding"]

            session = tools.Session(
                session_id=session_spec.get("id"),
                cwd=sandbox.text(session_spec.get("cwd")),
                surface=session_spec.get("surface"),
                profile=case.get("profile"),
                config=_session_config(tools, case.get("config") or {}, sandbox),
                state_get=state.get,
                state_set=lambda name, value: state.__setitem__(name, value),
                clock=lambda: clock,
            )

            result = tools.call_tool(tool, request, session)

            expected = sandbox.many(expected, after=True)
            if state_after is not _MISSING:
                state_after = sandbox.many(state_after, after=True)
            if files_after is not _MISSING:
                files_after = sandbox.many(files_after, after=True)

            _check_envelope(expected, result)
            _check_safety(result, root, home)
            if expected.get("ok") is False:
                _check_records_untouched(before, root)

            if state_after is not _MISSING:
                _check_state(key, session_spec.get("id"), state_after, state)
            if files_after is not _MISSING:
                _check_files(root, files_after)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


_MISSING = object()


def _temp_dir():
    import tempfile

    return Path(tempfile.mkdtemp(prefix="golden-", dir=os.environ.get("TMPDIR") or None))


def _copy_fixture(name, scratch):
    source = FIXTURES / name
    if not source.is_dir():
        raise AssertionError(f"fixture {name!r} is not a directory under tests/fixtures/")
    root = scratch / "library"
    shutil.copytree(source, root)
    return Path(os.path.realpath(root))


def _make_home(scratch):
    home = scratch / "home"
    home.mkdir()
    return Path(os.path.realpath(home))


class _home:
    """Point ``os.path.expanduser('~')`` at the temp home for the call."""

    def __init__(self, path):
        self.path = str(path)
        self.previous = _MISSING

    # expanduser reads HOME on POSIX and USERPROFILE on Windows.
    _NAMES = ("HOME", "USERPROFILE")

    def __enter__(self):
        self.previous = {name: os.environ.get(name, _MISSING) for name in self._NAMES}
        for name in self._NAMES:
            os.environ[name] = self.path
        return self

    def __exit__(self, *exc_info):
        for name, value in self.previous.items():
            if value is _MISSING:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        return False


def _parse_clock(text):
    if not isinstance(text, str):
        raise AssertionError("clock must be an ISO datetime string")
    value = datetime.fromisoformat(text)
    if value.tzinfo is None or value.utcoffset() is None:
        raise AssertionError("clock must be an aware ISO datetime")
    return value


def _library_id(root):
    path = root / ".engagements" / "library.yaml"
    if not path.is_file():
        return None
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if isinstance(loaded, dict) and isinstance(loaded.get("library_id"), str):
        return loaded["library_id"]
    return None


def _session_config(tools, config, sandbox):
    unknown = set(config) - CONFIG_KEYS
    if unknown:
        raise AssertionError(f"unknown config keys: {sorted(unknown)}")
    library_root = sandbox.text(config.get("library_root", str(sandbox.root)))
    if not os.path.isabs(library_root):
        library_root = os.path.join(str(sandbox.root), library_root)
    labels = dict(DEFAULT_LABELS)
    labels.update(config.get("labels") or {})
    return tools.SessionConfig(
        library_root=library_root,
        labels=labels,
        auto_bind=config.get("auto_bind", True),
        banner=config.get("banner", True),
        create_default=config.get("create_default", False),
    )


# --- setup ------------------------------------------------------------------------


def _apply_setup(root, home, setup, clock, writer):
    if not isinstance(setup, list):
        raise AssertionError("setup must be a list")
    for index, op in enumerate(setup):
        where = f"setup[{index}]"
        if not isinstance(op, dict) or op.get("op") not in SETUP_OPS:
            raise AssertionError(f"{where}: unknown op {op!r}")
        name = op["op"]
        path = _target(root, home, op["path"], where)
        if name == "write":
            text = _setup_text(op, where)
            _write_bytes(path, text)
        elif name == "append":
            text = _setup_text(op, where)
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "ab") as handle:
                handle.write(text)
        elif name == "replace":
            old = op["old"].encode("utf-8")
            new = op["new"].encode("utf-8")
            data = path.read_bytes()
            if data.count(old) != 1:
                raise AssertionError(f"{where}: old text occurs {data.count(old)} times in {op['path']}")
            _write_bytes(path, data.replace(old, new, 1))
        elif name == "delete":
            path.unlink()
        elif name == "mkdir":
            path.mkdir(parents=True, exist_ok=True)
        elif name == "symlink":
            path.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(op["target"], path)
        elif name == "snapshot":
            writer.read_record(root, op["path"], clock=lambda: clock)


def _setup_text(op, where):
    if "hex" in op:
        return bytes.fromhex(op["hex"])
    text = op.get("text")
    if not isinstance(text, str):
        raise AssertionError(f"{where}: write/append needs text or hex")
    repeat = op.get("repeat", 1)
    if not isinstance(repeat, int) or repeat < 1:
        raise AssertionError(f"{where}: repeat must be a positive integer")
    return (text * repeat).encode("utf-8")


def _write_bytes(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(data if isinstance(data, bytes) else data.encode("utf-8"))


def _target(root, home, relpath, where):
    if not isinstance(relpath, str) or not relpath:
        raise AssertionError(f"{where}: path must be a non-empty string")
    text = relpath.replace("<root>", str(root)).replace("<home>", str(home))
    if _ANY_PLACEHOLDER.search(text):
        raise AssertionError(f"{where}: setup paths cannot use rev placeholders: {relpath!r}")
    if os.path.isabs(text):
        return Path(text)
    return root / text


# --- placeholders -----------------------------------------------------------------


class _Sandbox:
    """The temp library root, the temp home and the fixture a case runs against."""

    def __init__(self, root, home, fixture):
        self.root = root
        self.home = home
        self.fixture = fixture

    def text(self, value, *, after=False):
        if not isinstance(value, str):
            return value
        return _PLACEHOLDER.sub(lambda match: self._one(match, after), value)

    def many(self, value, *, after=False):
        """Substitute placeholders through every string of a nested structure."""
        if isinstance(value, str):
            return self.text(value, after=after)
        if isinstance(value, list):
            return [self.many(item, after=after) for item in value]
        if isinstance(value, dict):
            return {key: self.many(item, after=after) for key, item in value.items()}
        return value

    def optional(self, case, key):
        if key not in case:
            return _MISSING
        return self.many(case[key])

    def _one(self, match, after):
        if match.group(3) == "root":
            return str(self.root)
        if match.group(3) == "home":
            return str(self.home)
        if match.group(3) == "message":
            return "<message>"
        kind, relpath = match.group(1), match.group(2)
        if (kind == "rev_after") != after:
            return match.group(0)
        if kind == "fixture_rev":
            file = FIXTURES / self.fixture / relpath
            if not file.is_file():
                raise AssertionError(f"<fixture_rev:{relpath}>: no such file in fixture {self.fixture!r}")
            return _rev(file.read_bytes())
        file = Path(relpath) if os.path.isabs(relpath) else self.root / relpath
        if not file.is_file():
            when = "after the call" if after else "before the call"
            raise AssertionError(f"<{kind}:{relpath}>: no such file {when}")
        return _rev(file.read_bytes())


def _rev(data):
    return hashlib.sha256(data).hexdigest()[:16]


# --- comparison -------------------------------------------------------------------


def _check_envelope(expected, result):
    problems = []
    _compare(expected, result, "result", None, problems)
    if problems:
        raise AssertionError("golden envelope mismatch:\n" + "\n".join(problems))


def _compare(expected, actual, where, key, problems):
    if key == "message" and expected == "<message>":
        if not (isinstance(actual, str) and actual and "\n" not in actual and "\r" not in actual):
            problems.append(f"{where}: expected a non-empty single-line message, got {actual!r}")
        return
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            problems.append(f"{where}: expected an object, got {_short(actual)}")
            return
        for name in expected:
            if name not in actual:
                problems.append(f"{where}.{name}: missing from the result")
        for name in actual:
            if name not in expected:
                problems.append(f"{where}.{name}: unexpected key in the result ({_short(actual[name])})")
        for name in expected:
            if name in actual:
                _compare(expected[name], actual[name], f"{where}.{name}", name, problems)
        return
    if isinstance(expected, list):
        if not isinstance(actual, list):
            problems.append(f"{where}: expected a list, got {_short(actual)}")
            return
        if len(expected) != len(actual):
            problems.append(f"{where}: expected {len(expected)} entries, got {len(actual)}")
        for index, item in enumerate(expected[: len(actual)]):
            _compare(item, actual[index], f"{where}[{index}]", None, problems)
        return
    if expected != actual or type(expected) is not type(actual):
        problems.append(f"{where}: expected {_short(expected)}, got {_short(actual)}")


def _short(value):
    text = repr(value)
    return text if len(text) <= 200 else text[:197] + "..."


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(key)
            yield from _strings(item)


def _check_safety(result, root, home):
    if not isinstance(result, dict):
        raise AssertionError(f"the handler returned {type(result).__name__}, not an envelope object")
    try:
        dumped = json.dumps(result)
    except (TypeError, ValueError):
        raise AssertionError("the result is not JSON-serializable") from None
    if "traceback" in dumped.casefold():
        raise AssertionError("the result mentions a traceback")
    trouble = []
    for label in ("error", "warnings"):
        for text in _strings(result.get(label)):
            if str(root) in text or str(home) in text or _ABSOLUTE.search(text):
                trouble.append(text)
    if trouble:
        raise AssertionError(f"an error or warning names an absolute path: {trouble}")


def _record_bytes(root):
    found = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames if not name.startswith("."))
        for name in sorted(filenames):
            if name.startswith(".") or not name.endswith(".md"):
                continue
            full = os.path.join(dirpath, name)
            relative = os.path.relpath(full, root).replace(os.sep, "/")
            with open(full, "rb") as handle:
                found[relative] = handle.read()
    return found


def _check_records_untouched(before, root):
    after = _record_bytes(root)
    changed = sorted(name for name in before if after.get(name) != before[name])
    added = sorted(set(after) - set(before))
    if changed or added:
        raise AssertionError(f"a failed call changed record files: changed={changed} added={added}")


def _check_state(key, session_id, expected, state):
    if session_id is None:
        if expected is not None:
            raise AssertionError("state_after cannot be checked without a session id")
        return
    if key is None:
        if expected is not None:
            raise AssertionError("state_after expected a value but the fixture has no library id")
        return
    actual = state.get(key)
    problems = []
    _compare(expected, actual, "state_after", None, problems)
    if problems:
        raise AssertionError("golden state mismatch:\n" + "\n".join(problems))


def _check_files(root, files_after):
    if not isinstance(files_after, dict):
        raise AssertionError("files_after must be an object")
    problems = []
    for relpath, expected in files_after.items():
        file = Path(relpath) if os.path.isabs(relpath) else root / relpath
        if not file.is_file():
            problems.append(f"{relpath}: missing after the call")
            continue
        actual = file.read_bytes().decode("utf-8")
        if actual != expected:
            problems.append(f"{relpath}: expected {_short(expected)}, got {_short(actual)}")
    if problems:
        raise AssertionError("golden files_after mismatch:\n" + "\n".join(problems))


# --- the self-check ---------------------------------------------------------------


def check_cases():
    """Every problem with the case files themselves, as a list of strings.

    Covers the format README's rules that can be checked without running a
    tool: known tool directory, known top-level keys, required keys, a real
    fixture, a parseable aware clock and well-formed placeholders.
    """
    problems = []
    for tool, path in case_files():
        where = f"{tool}/{path.name}"
        if tool not in KNOWN_TOOLS:
            problems.append(f"{where}: {tool!r} is not a known tool")
        try:
            case = load_case(path)
        except (OSError, ValueError) as exc:
            problems.append(f"{where}: cannot be read ({exc})")
            continue
        unknown = set(case) - TOP_LEVEL_KEYS
        if unknown:
            problems.append(f"{where}: unknown top-level keys {sorted(unknown)}")
        missing = [key for key in REQUIRED_KEYS if key not in case]
        if missing:
            problems.append(f"{where}: missing required keys {missing}")
            continue
        if not isinstance(case.get("description"), str) or not case["description"].strip():
            problems.append(f"{where}: needs a one-line description")
        fixture = case["fixture"]
        if not isinstance(fixture, str) or not (FIXTURES / fixture).is_dir():
            problems.append(f"{where}: fixture {fixture!r} is not a directory under tests/fixtures/")
        try:
            _parse_clock(case["clock"])
        except (AssertionError, ValueError) as exc:
            problems.append(f"{where}: {exc}")
        session = case["session"]
        if not isinstance(session, dict):
            problems.append(f"{where}: session must be an object")
        else:
            wrong = set(session) - SESSION_KEYS
            if wrong:
                problems.append(f"{where}: unknown session keys {sorted(wrong)}")
            if "id" not in session:
                problems.append(f"{where}: session needs an id (null for no session)")
            stored = session.get("binding")
            if stored is not None and not (isinstance(stored, dict) and stored.get("mode") in ("auto", "explicit", "off")):
                problems.append(f"{where}: session.binding must be null or a stored binding object")
            elif isinstance(stored, dict) and stored["mode"] == "explicit" and not isinstance(stored.get("client"), str):
                problems.append(f"{where}: an explicit binding needs a client id")
        config = case.get("config") or {}
        if not isinstance(config, dict):
            problems.append(f"{where}: config must be an object")
        else:
            wrong = set(config) - CONFIG_KEYS
            if wrong:
                problems.append(f"{where}: unknown config keys {sorted(wrong)}")
        if not isinstance(case["request"], dict):
            problems.append(f"{where}: request must be an object")
        if not isinstance(case["expected"], dict):
            problems.append(f"{where}: expected must be an object")
        setup = case.get("setup") or []
        if not isinstance(setup, list):
            problems.append(f"{where}: setup must be a list")
        else:
            for index, op in enumerate(setup):
                if not isinstance(op, dict) or op.get("op") not in SETUP_OPS:
                    problems.append(f"{where}: setup[{index}] is not a known op")
                    continue
                if not isinstance(op.get("path"), str) or not op["path"]:
                    problems.append(f"{where}: setup[{index}] needs a path")
                if op["op"] == "replace" and not op.get("old"):
                    problems.append(f"{where}: setup[{index}] replace needs a non-empty old string")
                if op["op"] in ("write", "append") and "text" not in op and "hex" not in op:
                    problems.append(f"{where}: setup[{index}] needs text or hex")
        problems.extend(f"{where}: {problem}" for problem in _placeholder_problems(case))
    return problems


def _placeholder_problems(case):
    problems = []
    for chunk in ("request", "expected", "state_after", "files_after", "setup"):
        for text in _strings(case.get(chunk)):
            for match in _ANY_PLACEHOLDER.finditer(text):
                if not _PLACEHOLDER.fullmatch(match.group(0)):
                    problems.append(f"{chunk}: malformed placeholder {match.group(0)!r}")
            without = _ANY_PLACEHOLDER.sub("", text)
            if "<" in without:
                problems.append(f"{chunk}: stray '<' in {text!r}")
    return problems
