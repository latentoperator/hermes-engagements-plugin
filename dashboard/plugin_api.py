"""Engagements dashboard backend: read-only routes under ``/api/plugins/engagements/`` (SPEC 6.7).

Hermes mounts this router for both the Desktop page and the web dashboard tab, inside the
process the window is connected to, so it reads the same profile's settings, state and library
the agent uses. Every route is a GET and nothing here writes: the plugin state file is read
only when it already exists (never through the host lock, which a read would create), the
library is opened by the read-only opener that creates no file, directory or lock, and
records are read without a snapshot. The agent and the user's editor remain the only write
paths.

Only public Hermes APIs are used (``hermes_cli.config``, ``hermes_cli.plugins_state``,
``hermes_constants``). The plugin's core package is loaded from this folder's parent by file
path under a private module name, so nothing is added to ``sys.path``.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import re
import sys
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException

PLUGIN_ID = "engagements"
_PACKAGE = Path(__file__).resolve().parent.parent / "engagements"
_CORE = "hermes_dashboard_engagements_core"
_UNSET = object()

# The core's record id form (``library._ID_PATTERN``: 2 to 63 characters). Checked before any
# lookup, so a route never walks the filesystem for a value the library could not hold.
_RECORD_ID = re.compile(r"[a-z0-9][a-z0-9-]{1,62}")
# Every session id form the host uses (``20260811_120000_<hex>`` from ``new_session_id``,
# ``local-<sha256>``, uuid) fits this; it only has to be bounded, printable and not a path.
_SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@+~-]{0,199}")

router = APIRouter()


def _core(name):
    """``engagements.<name>`` from this plugin's own folder, imported once per process."""
    package = sys.modules.get(_CORE)
    if package is None:
        spec = importlib.util.spec_from_file_location(_CORE, _PACKAGE / "__init__.py",
                                                      submodule_search_locations=[str(_PACKAGE)])
        package = importlib.util.module_from_spec(spec)
        sys.modules[_CORE] = package
        try:
            spec.loader.exec_module(package)
        except BaseException:
            sys.modules.pop(_CORE, None)
            raise
    return importlib.import_module(f"{_CORE}.{name}")


def _get_setting(key, default=None):
    """What ``ctx.get_config(key, default)`` returns: ``plugins.entries.engagements.settings.<key>``."""
    from hermes_cli.config import load_config_readonly

    entry = (((load_config_readonly() or {}).get("plugins") or {}).get("entries") or {}).get(PLUGIN_ID)
    if not isinstance(entry, dict):
        return default
    for subtree in ("settings", "config"):  # ``config`` is Hermes's legacy fallback for the same values
        value = entry.get(subtree)
        for segment in key.split("."):
            value = value.get(segment, _UNSET) if isinstance(value, dict) else _UNSET
        if value is not _UNSET:
            return value
    return default


def _default_root():
    """``<HERMES_HOME>/plugin-data/engagements/library``, computed without creating anything."""
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home()) / "plugin-data" / PLUGIN_ID / "library"


def _state_get(key):
    """The value stored under ``key``, read without creating a file (SPEC 6.7).

    ``PluginState.get`` opens ``.state.json.lock`` with ``a+b`` even for a read, so a GET
    on a profile that never wrote state would create that lock.
    ``PluginState.path`` is public, the host replaces the file atomically so a whole-file
    read is never a torn one, and a missing file means there is no binding. Only the
    file's own bytes are read: nothing is created, and no lock is taken.
    """
    from hermes_cli.plugins_state import PluginState

    path = PluginState(PLUGIN_ID).path
    try:
        with open(path, encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise OSError("plugin state could not be read") from exc
    if not isinstance(data, dict):
        raise OSError("plugin state could not be read")
    return data.get(key)


def _host():
    runtime = _core("runtime")
    data = _core("dashboard_data")
    return runtime.Host(
        get_setting=_get_setting,
        default_root=_default_root,
        state_get=lambda key: _state_get(runtime.state_key(key)),
        state_set=data.refuse_state_set,
        session_cwd=lambda: None,
        session_surface=lambda: None,
        profile=lambda: None,
        clock=lambda: datetime.now().astimezone(),
    )


def _call(name, *args):
    """Run one read-only view; a failure is a short message, never a traceback."""
    data = _core("dashboard_data")
    errors = _core("errors")
    try:
        return getattr(data, name)(_host(), *args)
    except errors.EngagementsError as exc:
        raise HTTPException(status_code=503, detail=exc.message) from None
    except Exception:  # noqa: BLE001 - the dashboard shows a sentence, the log keeps nothing sensitive
        raise HTTPException(status_code=500, detail="Engagements could not read the library.") from None


def _record_id(value):
    """The core's record id form, checked before any lookup (spec 6.7).

    Every id the library can hold matches it, so a refused value is one no lookup could
    find. The error names the rule, not the caller's text.
    """
    if not isinstance(value, str) or _RECORD_ID.fullmatch(value) is None:
        raise HTTPException(status_code=400, detail="Invalid record id.")
    return value


def _session_id(value):
    """A session id the host could issue, checked before it becomes a state key.

    ``runtime.state_key`` hashes a key it cannot use as-is, so a malformed id would
    otherwise be digested into a state key instead of being refused. Every host form
    fits the pattern; nothing here is reflected back in the error.
    """
    text = (value or "").strip()
    if not text:
        return None
    if _SESSION_ID.fullmatch(text) is None or ".." in text:
        raise HTTPException(status_code=400, detail="Invalid session id.")
    return text


@router.get("/overview")
def overview():
    return _call("overview")


@router.get("/record/{record_id}")
def record(record_id: str):
    result = _call("record", _record_id(record_id))
    if result is None:
        raise HTTPException(status_code=404, detail=f"No client or project with id '{record_id}'.")
    # Two records under one id: the id names neither of them, so this is not a 404 (the id exists)
    # and not a record (one of two would be a wrong answer). The body is never sent.
    if result.get("status") == "conflict":
        raise HTTPException(status_code=409, detail=result["message"])
    return result


@router.get("/session")
def session(id: str = ""):  # noqa: A002 - the query parameter is called id
    return _call("session_view", _session_id(id))


@router.get("/doctor")
def doctor():
    return _call("doctor_view")
