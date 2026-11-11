"""Source-level guarantees for the writer (AGENTS.md, spec A1/A2)."""
import ast
import re
from pathlib import Path

SOURCE = Path(__file__).resolve().parent.parent / "engagements" / "writer.py"


def imported_modules():
    tree = ast.parse(SOURCE.read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(((node.module or "").split(".")[0]) if node.level == 0 else "<relative>")
    return names


def test_writer_imports_nothing_from_hermes_or_spike():
    forbidden = {"hermes", "hermes_cli", "agent", "gateway", "tools", "spike"}
    assert not imported_modules() & forbidden


def test_writer_depends_only_on_the_stdlib_and_the_package():
    allowed = {"<relative>", "contextlib", "errno", "os", "re", "stat", "sys", "tempfile", "collections", "dataclasses",
               "datetime", "pathlib", "unicodedata", "typing", "__future__"}
    assert imported_modules() <= allowed


def test_writer_never_reads_the_system_clock():
    text = SOURCE.read_text()
    for pattern in (r"datetime\.now", r"datetime\.utcnow", r"date\.today", r"time\.time\(", r"time\.localtime",
                    r"time\.gmtime", r"time\.strftime"):
        assert not re.search(pattern, text), pattern


def test_writer_has_no_process_global_lock_or_session_state():
    text = SOURCE.read_text()
    assert "lockf" not in text and "F_SETLK" not in text
    assert not re.search(r"^_?[A-Za-z_]*(LOCKS|CACHE|SESSION|CWD|PROFILE)[A-Za-z_]*\s*=\s*(\{|dict\(|\[)", text, re.M)
    assert "os.getcwd" not in text and "os.chdir" not in text
