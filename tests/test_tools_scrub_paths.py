"""Diagnostics never show an absolute local path, on any host: POSIX and Windows forms alike."""

import pytest
from engagements import tools

ROOTS = ["/srv/library", "C:\\Users\\ann\\Library", "\\\\?\\C:\\Users\\ann\\Library"]


@pytest.mark.parametrize(("text", "expected"), [
    ("No match for '/srv/library/Acme Bakery/Client.md'.", "No match for 'Acme Bakery/Client.md'."),
    ("No match for 'C:\\Users\\ann\\Library\\Acme\\Client.md'.", "No match for 'Acme/Client.md'."),
    ("No match for 'c:\\users\\ann\\library\\Acme\\Client.md'.", "No match for 'Acme/Client.md'."),
    ("No match for 'C:/Users/ann/Library/Acme/Client.md'.", "No match for 'Acme/Client.md'."),
    ("at \\\\?\\C:\\Users\\ann\\Library\\Acme\\Client.md now", "at Acme/Client.md now"),
    ("root C:\\Users\\ann\\Library", "root ."),
    ("elsewhere C:\\Windows\\System32\\secret.txt", "elsewhere secret.txt"),
    ("share \\\\fileserver\\public\\notes.md", "share notes.md"),
    ("plain Acme Bakery/Client.md stays", "plain Acme Bakery/Client.md stays"),
    ("a word C: and a/b stay", "a word C: and a/b stay"),
])
def test_absolute_paths_become_relative_or_a_bare_name(text, expected):
    assert tools._scrub_text(text, ROOTS) == expected
