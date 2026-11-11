"""Shared fixtures for the writer tests: a fictional library and injected clocks."""
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from engagements import writer

CENTRAL = timezone(timedelta(hours=-6))

ACME = """---
engagements: client
id: acme-bakery
name: Acme Bakery
aliases: [Acme]
status: active
updated: 2026-11-05
---

# Acme Bakery

## Summary
Three-location bakery. Owner-run; values speed over polish.

## Current work
- Website refresh (see project `acme-website-2026`)

## Contacts
- Jane Rivera | owner, final approver | jane@acme-bakery.example | email preferred, no calls before 10am

## Communication preferences
- Weekly status by email on Fridays

## Locations
- Shared docs: ~/clients/acme/docs

## Systems of record
- Invoicing: accounting customer "Acme Bakery" (reference only)

## History

## Unconfirmed
- Prefers Friday updates (source: kickoff notes 2026-11-05, assumed)

## Change log
- 2026-11-05 owner: created from onboarding conversation
"""

WEBSITE = """---
engagements: project
id: acme-website-2026
client: acme-bakery
name: Website 2026
status: active
locations:
  - ~/clients/acme/website-2026
  - https://git.example/studio/acme-site
updated: 2026-11-05
---

## Purpose
Replace the 2019 site; launch before spring menu.

## Notes

## Change log
"""

SAM = "- Sam Ortiz | manager, Elm St | sam@acme-bakery.example"
JANE_LINE = "jane@acme-bakery.example"


def fixed_clock():
    return datetime(2026, 11, 7, 14, 2, tzinfo=CENTRAL)


def ticking_clock(start=None, seconds=1):
    """A clock that advances on every call so timestamps stay distinct."""
    state = {"now": start or fixed_clock()}

    def clock():
        current = state["now"]
        state["now"] = current + timedelta(seconds=seconds)
        return current

    return clock


def client_exists(value):
    return value in {"acme-bakery", "riverbend-dental"}


@dataclass
class Library:
    root: Path
    path: Path

    @property
    def meta(self):
        return self.root / ".engagements"

    def read(self):
        return self.path.read_bytes()

    def edit(self, old, new, *, count=1):
        """Simulate an external editor save."""
        data = self.read().decode("utf-8")
        assert old in data, old
        self.path.write_bytes(data.replace(old, new, count).encode("utf-8"))
        return self.read()

    def base(self):
        """A plugin read of the current bytes: returns the rev, snapshot written."""
        return writer.read_record(self.root, self.path)[1]

    def history(self):
        directory = self.meta / "history" / "acme-bakery"
        return sorted(directory.glob("*.md")) if directory.is_dir() else []

    def leftovers(self):
        return sorted(p.name for p in self.path.parent.iterdir() if p.name != self.path.name)


def make_library(tmp_path, text=ACME, *, name="Client.md", folder="Acme Bakery"):
    root = tmp_path / "library"
    (root / ".engagements").mkdir(parents=True)
    (root / ".engagements" / "library.yaml").write_text("format: 1\nlibrary_id: 11111111-1111-4111-8111-111111111111\n")
    directory = root / folder
    directory.mkdir()
    path = directory / name
    path.write_bytes(text.encode("utf-8") if isinstance(text, str) else text)
    return Library(root=root, path=path)


def do_update(lib, base_rev, ops, **overrides):
    arguments = {
        "kind": "client",
        "profile": "studio-dev",
        "clock": fixed_clock,
        "client_exists": client_exists,
    }
    arguments.update(overrides)
    source = arguments.pop("source", "owner email 2026-11-07")
    path = arguments.pop("path", lib.path)
    return writer.update(lib.root, path, base_rev, source, ops, **arguments)


def op_contacts_replace(text="- Jane Rivera | owner | jane@acmebakery.example"):
    return {"op": "replace_line", "section": "Contacts", "match": JANE_LINE, "text": text}


def op_comm_append(text="- Monthly review call"):
    return {"op": "append_to_section", "section": "Communication preferences", "text": text}


def op_status(value="paused"):
    return {"op": "set_property", "key": "status", "value": value}


def tree_snapshot(root):
    """Every file under root with its bytes, for proving nothing was written.

    Lock files are excluded: they are empty coordination files, not record data.
    """
    result = {}
    for directory, _, names in os.walk(root):
        if Path(directory).name == "locks":
            continue
        for name in names:
            path = Path(directory) / name
            if path.is_symlink():
                continue
            result[str(path.relative_to(root))] = path.read_bytes()
    return result
