"""Shared helpers for the engagements tool tests. Fictional data only."""

import hashlib
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

FIXTURES = Path(__file__).parent / "fixtures"
CLOCK_TIME = datetime(2026, 11, 7, 14, 2, tzinfo=timezone(timedelta(hours=-6)))


def fixed_clock():
    """The pinned clock every tool test uses: 2026-11-07T14:02:00-06:00."""
    return CLOCK_TIME


def make_session(root, *, session_id="sess-1", cwd=None, surface="cli", profile="studio-dev",
                 auto_bind=True, create_default=False, state=None, clock=None, labels=None):
    """A tools.Session over ``root`` plus the plain dict that backs its state."""
    from engagements.tools import Session, SessionConfig

    store = {} if state is None else state

    def state_get(key):
        return store.get(key)

    def state_set(key, value):
        store[key] = value

    config = SessionConfig(library_root=root, auto_bind=auto_bind, create_default=create_default)
    if labels is not None:
        config.labels = dict(labels)
    session = Session(session_id=session_id, cwd=cwd, surface=surface, profile=profile, config=config,
                      state_get=state_get, state_set=state_set, clock=clock or fixed_clock)
    return session, store


def copy_library(tmp_path, fixture="studio"):
    """A writable copy of a fixture library, returned as its resolved root."""
    root = Path(tmp_path) / "library"
    shutil.copytree(FIXTURES / fixture, root)
    return root.resolve()


def call(name, args, session):
    """Invoke one tool handler through the public entry point."""
    from engagements.tools import call_tool

    return call_tool(name, args, session)


def rev_of(path):
    """The 16-hex rev of a record file (spec 5.1)."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]


def library_id(root):
    document = yaml.safe_load((Path(root) / ".engagements" / "library.yaml").read_text(encoding="utf-8"))
    return document["library_id"]


def binding_key_for(root, session_id):
    """The per-session state key for a library root (spec 6.4)."""
    from engagements.binding import binding_key

    return binding_key(library_id(root), session_id)


def banner_key_for(session_id):
    """The per-session state key that remembers the last banner emitted to it (spec A4)."""
    return f"banner:{session_id}"


def stored_binding(state, root, session_id):
    """The value stored under this session's binding key, or None."""
    return state.get(binding_key_for(root, session_id))
