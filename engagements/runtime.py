"""Host-side plugin logic with every Hermes dependency injected (spec 6.1 to 6.4, Phase 2).

The root ``__init__.py`` is the only module that touches Hermes. It builds a
``Host`` from the plugin context and calls the functions here, so every hook,
slash command, CLI action and tool wrapper is unit-testable with plain
callables. Nothing here imports Hermes, reads the clock, or keeps process-global
session state (spec A1): each call receives its session id, cwd, surface,
settings and state store through the ``Host``.

Hooks and commands never raise into the host and never carry a traceback:
failures become a skipped hook, a plain sentence, or a tool error envelope.
"""

import contextlib
import hashlib
import json
import logging
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from . import binding, doctor
from .errors import EngagementsError
from .library import open_library
from .tools import Session, SessionConfig, call_tool, session_binding

logger = logging.getLogger(__name__)

DEFAULT_LABELS = {"client": "Client", "project": "Project"}
LABEL_LIMIT = binding.LABEL_LIMIT
DOCTOR_LINE_LIMIT = 60
LIST_LIMIT = 100
_STATE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_TRUE = frozenset({"true", "yes", "on", "1"})
_FALSE = frozenset({"false", "no", "off", "0"})
_FALLBACK = json.dumps({"ok": False, "error": {"code": "io_error", "message": "Filesystem operation failed.",
                                               "details": {}}, "warnings": []})


@dataclass
class Host:
    """What the plugin needs from its host, as plain callables (spec A1)."""

    get_setting: Callable[[str, Any], Any]  # ctx.get_config(key, default): profile-scoped settings
    default_root: Callable[[], Any]  # plugin_data_dir("engagements") / "library" (spec A7)
    state_get: Callable[[str], Any]  # ctx.state.get, profile-scoped
    state_set: Callable[[str, Any], None]  # ctx.state.set, profile-scoped
    session_cwd: Callable[[], str]  # ctx.session_cwd(): the calling session's cwd, never os.getcwd()
    session_surface: Callable[[], str]  # ctx.session_surface(): cli, tui, desktop, a messaging platform
    profile: Callable[[], str]  # ctx.profile_name
    clock: Callable[[], datetime]  # an aware datetime; the core never reads the clock
    # Tells an open dashboard that the library or a binding changed (SPEC 6.7). Best effort:
    # it never affects a tool result, and a host without a dashboard leaves it unset.
    changed: Callable[[str], None] | None = None


@dataclass
class PluginConfig(SessionConfig):
    """``SessionConfig`` plus why a configured value was not used, for commands to say so."""

    problem: str | None = None
    default_location: bool = False  # library_root is the default location (kept when create_default is off)


# --- settings (spec 6.1) ---------------------------------------------------------------------


def _setting(host, key, default):
    try:
        value = host.get_setting(key, default)
    except Exception:  # noqa: BLE001 - an unreadable display setting is its default, never an error into the model
        return default
    return default if value is None else value


def _library_setting(host):
    """``(value, readable)`` for ``library_root``.

    # SPEC-QUESTION: an unreadable setting is not an absent one. Treating it as absent would choose
    # the default location, so the first create would write into a library the profile never
    # configured. The caller fails closed instead: no root, no write.
    """
    try:
        value = host.get_setting("library_root", None)
    except Exception:  # noqa: BLE001 - the caller reports it as an unavailable location
        return None, False
    return (None if value is None else value), True


def _as_bool(value, default):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
    return default


_STORED_ID = re.compile(r"[a-z0-9][a-z0-9-]{1,62}")  # the library's id form (library._ID_PATTERN)


def _stored_id(value):
    """A stored binding id for display: only the library's own id form, else a fixed placeholder."""
    return value if isinstance(value, str) and _STORED_ID.fullmatch(value) else "(unreadable id)"


def _label(value, default):
    if isinstance(value, str) and value.strip() and len(value.strip()) <= LABEL_LIMIT:
        text = " ".join(value.split())
        if text.isprintable():
            return text
    return default


def _root_from(host, configured):
    """``(root, create_default, problem)`` for the configured ``library_root`` or the default one."""
    if configured is None or (isinstance(configured, str) and not configured.strip()):
        try:
            return Path(os.fspath(host.default_root())), True, None
        except Exception:  # noqa: BLE001 - no default location is a missing library, not a crash
            return None, False, "default_root_unavailable"
    if not isinstance(configured, (str, os.PathLike)):
        return None, False, "library_root_invalid"
    text = os.path.expanduser(os.fspath(configured).strip())
    # SPEC-QUESTION: a relative library_root is ambiguous. The plugin runs inside the shared gateway process
    # whose cwd is the Hermes install directory (A1), so it is refused instead of resolved against that.
    if not os.path.isabs(text):
        return None, False, "library_root_not_absolute"
    return Path(text), False, None


def resolve_config(host):
    """The profile's settings with defaults applied (spec 6.1).

    Precedence: a non-blank ``library_root`` setting, else the default location, which is the only
    one the first tool call creates (D10). An invalid value falls back to its default and, for the
    root, leaves it unset so ``library_missing`` is reported instead of guessing a location. A root
    setting that cannot be read at all also leaves it unset: an unavailable location must not become
    the default one, or a create would write where the profile did not configure it.
    """
    configured, readable = _library_setting(host)
    if readable:
        root, create_default, problem = _root_from(host, configured)
    else:
        root, create_default, problem = None, False, "library_root_unreadable"
    labels = {}
    for kind in ("client", "project"):
        labels[kind] = _label(_setting(host, f"labels.{kind}", DEFAULT_LABELS[kind]), DEFAULT_LABELS[kind])
    return PluginConfig(library_root=root, labels=labels, auto_bind=_as_bool(_setting(host, "auto_bind", True), True),
                        banner=_as_bool(_setting(host, "banner", True), True), create_default=create_default,
                        problem=problem, default_location=create_default)


# --- state store and session -------------------------------------------------------------------


def state_key(key):
    """A key Hermes accepts (1 to 128 characters of ``[A-Za-z0-9_.:-]``, no ``..``).

    # SPEC-QUESTION: spec A10 names the key ``binding:<library_id>:<session_id>``, but a session id
    # that is long or holds other characters would make Hermes refuse it. Such keys map to a stable
    # digest of the full key, so two sessions never share one and the common case is unchanged.
    """
    if _STATE_KEY.fullmatch(key) and ".." not in key:
        return key
    kind = re.sub(r"[^A-Za-z0-9_-]", "", key.partition(":")[0])[:20] or "key"
    return f"{kind}:h-{hashlib.sha256(key.encode('utf-8', 'surrogatepass')).hexdigest()[:48]}"


class _Store:
    """The plugin state store: Hermes failures surface as ``OSError``, which the core reports as ``io_error``."""

    def __init__(self, host):
        self._host = host

    def get(self, key):
        try:
            return self._host.state_get(state_key(key))
        except Exception as exc:  # noqa: BLE001
            raise OSError("plugin state could not be read") from exc

    def set(self, key, value):
        try:
            self._host.state_set(state_key(key), value)
        except Exception as exc:  # noqa: BLE001
            raise OSError("plugin state could not be written") from exc


def _call_text(call):
    try:
        value = call()
    except Exception:  # noqa: BLE001 - outside a session these have no answer
        return None
    return value.strip() if isinstance(value, str) and value.strip() else None


def build_session(host, session_id, config=None, surface=None):
    """The ``tools.Session`` for one call, built from kwargs and host callables only (spec A1).

    ``surface`` replaces the host's answer only for a slash command that resolved the ambiguous ``local``
    value from the session's own turn-verified surface (``_command_surface``); nothing else passes it.
    """
    config = resolve_config(host) if config is None else config
    store = _Store(host)
    profile = _call_text(host.profile)
    return Session(session_id=session_id if isinstance(session_id, str) and session_id else None,
                   cwd=_call_text(host.session_cwd), surface=surface or _call_text(host.session_surface),
                   profile=profile,
                   config=config, state_get=store.get, state_set=store.set, clock=host.clock)


def _quiet(config):
    """The same config for a hook or command: it may open a library, never create one."""
    return replace(config, create_default=False)


# --- tools -----------------------------------------------------------------------------------


_CHANGES = {"engagements_create": "library", "engagements_update": "library", "engagements_bind": "binding"}


def notify_changed(host, what):
    """Tell an open dashboard to refresh. Never raises and never changes what the caller returns."""
    if host.changed is None:
        return
    try:
        host.changed(what)
    except Exception:  # noqa: BLE001 - a dashboard refresh is a courtesy, not part of the contract
        logger.debug("engagements change notice skipped", exc_info=True)


def run_tool(host, name, args, session_id=None):
    """One tool call as the JSON text the model receives. The envelope is the core's; nothing else leaks."""
    try:
        session = build_session(host, session_id)
        result = call_tool(name, args, session)
        if result.get("ok") and name in _CHANGES:
            notify_changed(host, _CHANGES[name])
        return json.dumps(result, ensure_ascii=False, default=str)
    except Exception as exc:  # noqa: BLE001 - the contract: no traceback text reaches the model
        logger.warning("engagements tool %s failed: %s", name, type(exc).__name__)
        logger.debug("engagements tool failure detail", exc_info=True)
        return _FALLBACK


# --- hooks (spec 6.4) --------------------------------------------------------------------------


def _sid(value):
    return value if isinstance(value, str) and value.strip() else None


def _open_quiet(config):
    """The configured library if it exists; hooks never create one."""
    if config.library_root is None:
        return None
    try:
        return open_library(config.library_root, create_default=False)
    except EngagementsError:
        return None


def banner_key(session_id):
    """The session's banner record: the signature last emitted to it, whatever library sent it.

    Keyed by the session alone, because the record has to survive the two things that make a
    replayed banner false: the library going away and the configured library being switched.
    The library id changes in both cases, so a record keyed by it could not be found to retire
    the earlier banner.
    """
    return f"banner:{session_id}"


def _banner_state(store, session_id):
    value = store.get(banner_key(session_id))
    if not isinstance(value, dict):
        return None, False
    signature = value.get("sig")
    return (signature if isinstance(signature, str) else None), value.get("compacted") is True


def _remember_banner(store, session_id, signature, compacted):
    store.set(banner_key(session_id), {"sig": signature, "compacted": bool(compacted)})


# --- turn-verified surface (spec 6.4, 6.3) ---------------------------------------------------------
#
# A slash command reports the surface ``local`` for the classic CLI, the TUI and Desktop alike
# (RUNTIME-EVIDENCE Phases 3 and 3D), so the command cannot tell them apart. A turn can: tools and
# hooks report ``cli``, ``tui`` or ``desktop``. The per-turn hook keeps the last turn-verified surface
# of each session id, and the command uses it for that same id only.


def surface_key(session_id):
    return f"surface:{session_id}"


def _sync_surface(host, session_id, *, may_create):
    """Bring ``surface:<session_id>`` in line with this turn's surface, whatever state the library is in.

    A supported surface (``binding.CWD_SURFACES``) is recorded; any other or unknown one, including a
    surface the host could not answer, clears an existing record. Clearing is what revokes the
    authority, so it never depends on the library opening or on binding maintenance: it runs on every
    turn and writes only where a record exists, so a user who never configured anything still gets no
    plugin state from it.

    Recording is the other direction and is allowed to be conservative. A new record is written only
    when ``may_create`` says the library opened this turn, because a record with no library behind it
    is useless (``/client`` would have nothing to resolve) and would create state for a user who has
    not set the plugin up. An existing record is refreshed either way, so a session that moves between
    ``cli``, ``tui`` and ``desktop`` keeps the surface it last reported. Without a record ``/client``
    stays read-only, which is the safe side. Never raises: a failure to read or write leaves the turn
    unaffected and ``/client`` read-only (``_command_surface`` treats an unreadable record as absent).
    """
    try:
        surface = _call_text(host.session_surface)
        value = surface if surface in binding.CWD_SURFACES else None
        store = _Store(host)
        key = surface_key(session_id)
        stored = store.get(key)
        if stored == value or (value is not None and stored is None and not may_create):
            return
        store.set(key, value)
    except Exception:  # noqa: BLE001 - without the record /client stays read-only; the turn goes on
        logger.debug("engagements surface sync skipped", exc_info=True)


def _command_surface(host, session):
    """The surface a slash command may act on: its own if it can resolve cwd, else the verified one.

    Only the ambiguous ``local`` value is resolved, and only from what this session id's own turns
    reported. A session with no turn yet, or any other surface, keeps what the command reported.
    """
    if session.surface != "local" or session.session_id is None:
        return session.surface
    try:
        recorded = _Store(host).get(surface_key(session.session_id))
    except OSError:
        return session.surface
    return recorded if recorded in binding.CWD_SURFACES else session.surface


def _maintain_binding(host, session_id, config=None, surface=None):
    """Resolve this session's binding and retain an automatic identity (spec 6.4).

    Runs on every turn, whatever the ``banner`` setting: retention is what makes the next turn
    revalidate the automatic binding instead of resolving a substitute from the cwd. Presentation is
    decided separately, so switching the banner off changes nothing about which client is bound.
    Returns ``(library, state, store)`` and raises ``EngagementsError`` only when the library cannot
    be opened or the state store fails, so a reader can report that without a second resolution.

    No other setting gates this. ``labels`` only renames output; ``quiet`` only stops a hook creating
    a library; ``auto_bind`` is part of the core resolution, not a display choice. The hooks that return
    before resolving (compaction after migrating, reset and delete) only migrate or prune; the CLI
    subcommands, ``/client list`` and ``/engagements doctor`` never read the binding.
    """
    session = build_session(host, session_id, _quiet(resolve_config(host) if config is None else config), surface)
    library, state, stored = session_binding(session)
    store = _Store(host)
    after = binding.stored_after(stored, state)
    if after != stored:  # an automatic binding is remembered so the next turn revalidates it
        with contextlib.suppress(EngagementsError):
            binding.store_binding(store.set, library.library_id, session_id, after)
            notify_changed(host, "binding")
    return library, state, store


def _retire_banner(host, session_id, labels=None):
    """One notice retiring a banner the model may still be replaying, when the library cannot be read.

    Hermes replays the banner on every later request, so a turn that cannot open the library
    leaves the earlier claim standing unless it is answered. Only a banner that announced a
    binding needs answering, and only once: the answer is recorded through the same banner
    state the bound banner used, so the next unreadable turn says nothing and a later readable
    turn re-announces whatever is bound then.
    """
    store = _Store(host)
    last, _ = _banner_state(store, session_id)
    if not binding.announced_a_binding(last):
        return None
    with contextlib.suppress(OSError):  # worst case the next unreadable turn repeats the notice
        _remember_banner(store, session_id, binding.UNAVAILABLE_SIGNATURE, False)
    return binding.unavailable_text(labels)


def _banner(host, session_id):
    ready = False
    try:
        config = resolve_config(host)
        try:
            maintained = _maintain_binding(host, session_id, config)
        except EngagementsError:
            return _retire_banner(host, session_id, config.labels) if config.banner else None
        ready = True
    finally:
        # Outside the library gate on purpose: an unsupported turn must revoke the stored surface even
        # when the library is missing or unreadable, or the record outlives the surface it vouched for.
        _sync_surface(host, session_id, may_create=ready)
    if not config.banner:
        return None
    library, state, store = maintained
    last, compacted = _banner_state(store, session_id)
    text, signature = binding.next_banner(state, last, compacted, config.labels, library.library_id)
    if text is None:
        return None
    with contextlib.suppress(OSError):  # worst case the next turn repeats the banner
        _remember_banner(store, session_id, signature, False)
    return text or None


def banner_hook(host, **kwargs):
    """``pre_llm_call``: the banner when the binding state differs from the last one emitted (A4)."""
    session_id = _sid(kwargs.get("session_id"))
    if session_id is None:
        return None
    try:
        text = _banner(host, session_id)
    except Exception:  # noqa: BLE001 - a hook failure must never become a turn failure
        logger.debug("engagements banner skipped", exc_info=True)
        return None
    return {"context": text} if text else None


def compaction_hook(host, **kwargs):
    """``post_compaction``: migrate defensively, then mark a banner re-emit (A5). Kwargs only."""
    session_id = _sid(kwargs.get("session_id"))
    old_id = _sid(kwargs.get("old_session_id"))
    if session_id is None:
        return
    try:
        config = resolve_config(host)
        library = _open_quiet(config)
        if library is None:
            return
        store = _Store(host)
        rotated = old_id is not None and old_id != session_id
        if rotated:
            binding.migrate(store.get, store.set, library.library_id, old_id, session_id)
        if not config.banner:
            return
        signature, _ = _banner_state(store, session_id)
        if signature is None and rotated:
            signature, _ = _banner_state(store, old_id)
        _remember_banner(store, session_id, signature, True)
    except Exception:  # noqa: BLE001
        logger.debug("engagements compaction hook skipped", exc_info=True)


def _forget(store, library, session_id):
    """Set a session's binding and banner keys to null, only where something is stored.

    The banner key is keyed by the session alone, so it is cleared even when the library cannot
    be opened. Only the binding key needs the library id, so that half is skipped then rather
    than keeping the whole session's state.
    """
    names = [banner_key(session_id)]
    if library is not None:
        names.insert(0, binding.binding_key(library.library_id, session_id))
    for name in names:
        if store.get(name) is not None:
            store.set(name, None)


def _forget_surface(store, session_id):
    key = surface_key(session_id)
    if store.get(key) is not None:
        store.set(key, None)


def _hook_ids(kwargs, *names):
    """Every session id a hook payload names, in the order given, deduplicated.

    Hooks do not all name the session the same way. ``on_session_reset`` is recorded with a
    null ``session_id`` and the reset session in ``old_session_id`` (and an empty
    ``new_session_id``), while ``on_session_delete`` lists ``deleted_ids``. Reading only one
    field leaves the other ids' state behind, so each hook declares the fields it accepts and
    shares this collection.
    """
    ids = []
    for name in names:
        value = kwargs.get(name)
        for item in value if isinstance(value, (list, tuple)) else [value]:
            if isinstance(item, str) and item.strip() and item not in ids:
                ids.append(item)
    return ids


def reset_hook(host, **kwargs):
    """``on_session_reset``: the new session starts in ``auto`` with no banner history.

    The recorded payload carries ``session_id: null`` with the reset session in
    ``old_session_id``, so every id the payload names is pruned. The surface record and the
    library-scoped keys are forgotten independently, so neither an unreadable surface key nor
    a missing library keeps the other from being cleared.
    """
    ids = _hook_ids(kwargs, "session_id", "old_session_id", "new_session_id")
    if not ids:
        return
    store = _Store(host)
    for session_id in ids:
        try:
            _forget_surface(store, session_id)
        except Exception:  # noqa: BLE001 - one unreadable key must not keep the rest from being cleared
            logger.debug("engagements reset hook skipped a surface record", exc_info=True)
    try:
        library = _open_quiet(resolve_config(host))
    except Exception:  # noqa: BLE001 - without a library the session's own banner key is still cleared
        library = None
    for session_id in ids:
        try:
            _forget(store, library, session_id)
        except Exception:  # noqa: BLE001
            logger.debug("engagements reset hook skipped a session", exc_info=True)


def delete_hook(host, **kwargs):
    """``on_session_delete``: set each deleted session's binding key to null (A10; there is no delete API).

    Hermes supplies the complete deleted-id list, so every deduplicated id is pruned; a cap here
    would silently keep the state (and the binding) of the sessions it dropped.
    """
    ids = _hook_ids(kwargs, "session_id", "deleted_ids")
    if not ids:
        return
    store = _Store(host)
    for session_id in ids:  # one unreadable key must not keep the rest of the deleted ids from being pruned
        try:
            _forget_surface(store, session_id)
        except Exception:  # noqa: BLE001
            logger.debug("engagements delete hook skipped a surface record", exc_info=True)
    try:
        library = _open_quiet(resolve_config(host))
    except Exception:  # noqa: BLE001 - without a library the session's own banner key is still cleared
        library = None
    for session_id in ids:
        try:
            _forget(store, library, session_id)
        except Exception:  # noqa: BLE001
            logger.debug("engagements delete hook skipped a session", exc_info=True)


# --- slash commands (spec 6.3) -----------------------------------------------------------------

_NO_SESSION = ("No session id came with this command, so there is nothing to bind. "
               "Use the engagements_context tool with an explicit id to read a record, or "
               "engagements_find to look one up.")
_MISSING_LIBRARY = ("No library exists yet at {root}. It is created the first time the agent uses {entity} tool, "
                    "or run `hermes engagements init --root <path>` to choose a location.")
_MISSING_CONFIGURED = ("No library exists at {root}, the configured library_root. Create that folder or run "
                       "`hermes engagements init --root <path>` to choose a location.")
_NO_LOCATION = ("No library location is configured. Set library_root to an absolute path or run "
                "`hermes engagements init --root <path>`.")


def _who(state, labels):
    text = f"{labels['client']}: {state.client_name or state.client_id} ({state.client_id})"
    if state.project_id:
        text += f", {labels['project']}: {state.project_name or state.project_id} ({state.project_id})"
    return text


def render_state(state, labels, *, read_only=False, stored=None):
    """The ``/client`` status text: mode, binding and its evidence (spec 6.3).

    ``read_only`` is for a surface SPEC 6.4 does not allow to resolve the working directory. The text
    says so and reports only what is already stored, so a stored binding is never presented as this
    surface's automatic binding.
    """
    lines = [f"Mode: {state.mode}"]
    if state.state == "bound":
        lines.append(f"Bound{' (stored)' if read_only else ''}. {_who(state, labels)}")
    elif state.state == "ambiguous":
        ids = ", ".join(str(c.get("id")) for c in state.candidates)
        project_word = labels.get("project", "Project").lower()
        lines.append(f"Not bound: the working directory matches more than one {project_word} ({ids}).")
    elif state.state == "unbound" and state.reason == "discovery_incomplete":
        # The one place a /client status must not present a folder as settled: the
        # library could not be read in full, so even one match is not a binding.
        shown = ", ".join(state.unreadable[:3]) + ("..." if len(state.unreadable) > 3 else "")
        lines.append(f"Not bound: one or more records could not be read ({shown}), so the folder match "
                     "cannot be trusted. Run `hermes engagements doctor` or bind explicitly.")
    elif state.state == "invalid_binding":
        lines.append("Not bound: the stored binding is no longer valid.")
    elif state.state == "cwd_unsupported":
        lines.append("Not bound: automatic binding from the folder is not available on this surface."
                     if read_only else
                     "Not bound: this surface does not use the working directory for automatic binding.")
    else:
        lines.append("Not bound.")
    if read_only and state.state == "cwd_unsupported" and isinstance(stored, Mapping):
        client, project = stored.get("client"), stored.get("project")
        if isinstance(client, str) and client:
            # Stored ids are historical and unauthenticated: show only a well-formed id, never raw text.
            last = f"{labels['client'].lower()} {_stored_id(client)}"
            if isinstance(project, str) and project:
                last += f", {labels['project'].lower()} {_stored_id(project)}"
            lines.append(f"Last stored automatic binding: {last}.")
    lines.append(f"Evidence: {state.evidence}")
    if state.state != "bound":
        lines.append("Use /client <name-or-id> to bind, /client list to see what exists.")
    if read_only and state.state != "cwd_unsupported":
        lines.append("Automatic binding from the folder is not available on this surface.")
    return "\n".join(lines)


def _error_text(error):
    code, message = error.get("code"), error.get("message") or "The call failed."
    details = error.get("details") if isinstance(error.get("details"), dict) else {}
    if code == "ambiguous":
        names = ", ".join(f"{c.get('name') or c.get('id')} ({c.get('id')})" for c in details.get("candidates") or ()
                          if isinstance(c, dict))
        return f"{message} Candidates: {names}." if names else message
    if code == "invalid_op" and details.get("field"):
        return f"{message} ({details['field']}: {details.get('reason')})"
    if code == "library_missing" and details.get("reason") == "no_library_root":
        return _NO_LOCATION
    return message


def _bind_ref(session, ref):
    # SPEC-QUESTION: spec 6.3 says `/client <name-or-id>` binds a client. A name or id that matches no client is
    # tried as a project, which implies its client, so the command is no stricter than engagements_bind.
    result = call_tool("engagements_bind", {"client": ref}, session)
    if not result["ok"] and result["error"]["code"] == "not_found":
        retry = call_tool("engagements_bind", {"project": ref}, session)
        if retry["ok"] or retry["error"]["code"] != "not_found":
            return retry
    return result


def _status(host, session, config):
    """The ``/client`` status text, maintaining the binding it reports rather than reading a stale one.

    The working directory is resolved only on a surface in ``binding.CWD_SURFACES`` (spec 6.4). A slash
    command reports ``local`` on the CLI, the TUI and Desktop, so it acts on the surface this same
    session's own turns reported (``_command_surface``). With no such turn yet, or on any other surface,
    the status is read-only and writes no automatic binding state.
    """
    try:
        if session.surface in binding.CWD_SURFACES:
            _, state, _ = _maintain_binding(host, session.session_id, config, session.surface)
            return render_state(state, config.labels)
        _, state, stored = session_binding(session)
        return render_state(state, config.labels, read_only=True, stored=stored)
    except EngagementsError as exc:
        if exc.code == "library_missing":
            return missing_text(config)
        return _error_text({"code": exc.code, "message": exc.message, "details": exc.details})


def missing_text(config):
    """Why there is no library to read, for commands and the dashboard alike."""
    if config.library_root is None:
        return _NO_LOCATION
    default = getattr(config, "default_location", False) or config.create_default
    if not default:
        return _MISSING_CONFIGURED.format(root=os.fspath(config.library_root))
    # The sentence names the kind of tool the profile would use, so it takes the label.
    entity = binding.display_words(getattr(config, "labels", None), article=True)[0]
    return _MISSING_LIBRARY.format(root=os.fspath(config.library_root), entity=entity)


def list_result(session, labels):
    """``(ok, text)``: active clients with their active projects, and whether the lookup worked.

    The CLI needs the operation's status separately from the rendered text, so a failed open is a
    nonzero exit even though it still has a sentence to print.
    """
    result = call_tool("engagements_find", {"query": "", "limit": LIST_LIMIT}, session)
    if not result["ok"]:
        if result["error"]["code"] == "library_missing":
            return False, missing_text(session.config)
        return False, _error_text(result["error"])
    rows = result["data"]["results"]
    clients = [r for r in rows if r["kind"] == "client"]
    projects = [r for r in rows if r["kind"] == "project"]
    if not rows:
        return True, f"No {labels['client'].lower()}s yet. Ask the agent to add one, or paste your notes."
    known = {c["id"] for c in clients}
    lines = [f"Active {labels['client'].lower()}s and {labels['project'].lower()}s:"]

    def row(item, indent):
        flag = "" if item.get("valid", True) else "  (needs attention: run /engagements doctor)"
        return f"{indent}{item['name']} ({item['id']}){flag}"

    for client in clients:
        lines.append(row(client, "- "))
        lines.extend(row(p, "    - ") for p in projects if p["client"] == client["id"])
    orphans = [p for p in projects if p["client"] not in known]
    if orphans:
        lines.append(f"{labels['project']}s whose {labels['client'].lower()} is not listed:")
        lines.extend(row(p, "- ") for p in orphans)
    if result["data"].get("truncated"):
        lines.append(f"More records exist; showing {len(rows)}. Use engagements_find to narrow the search.")
    return True, "\n".join(lines)


def list_text(session, labels):
    """Active clients with their active projects (``/client list``): text only."""
    return list_result(session, labels)[1]


def client_command(host, raw_args, session_id=None):
    """``/client``: status, bind by name or id, ``off``, ``auto`` and ``list``. Returns text; never prints."""
    try:
        config = _quiet(resolve_config(host))
        labels = config.labels
        words = (raw_args or "").strip()
        session = build_session(host, _sid(session_id), config)
        verified = _command_surface(host, session)
        if verified != session.surface:
            session = build_session(host, session.session_id, config, verified)
        if words.lower() == "list":
            return list_text(session, labels)
        if session.session_id is None:
            return _NO_SESSION
        if not words:
            return _status(host, session, config)
        if words.lower() in ("off", "auto"):
            result = call_tool("engagements_bind", {"mode": words.lower()}, session)
        else:
            result = _bind_ref(session, words)
        if not result["ok"]:
            return "Could not change the binding: " + _error_text(result["error"])
        notify_changed(host, "binding")
        return "Binding updated.\n" + _status(host, session, config)
    except Exception:  # noqa: BLE001
        logger.debug("engagements /client failed", exc_info=True)
        return "Engagements could not complete that command."


def doctor_text(config, limit=DOCTOR_LINE_LIMIT):
    """The plain-text doctor report for the configured library, capped for a chat reply."""
    if config.library_root is None:
        return _NO_LOCATION
    lines = doctor.run_doctor(config.library_root).render_text().splitlines()
    if len(lines) > limit:
        hidden = len(lines) - limit + 1
        lines = lines[:limit - 1] + [f"... {hidden} more lines. Run `hermes engagements doctor` for the full report."]
    return "\n".join(lines)


def engagements_command(host, raw_args):
    """``/engagements doctor``: the validation report (spec 6.3)."""
    try:
        words = (raw_args or "").split()
        if words[:1] == ["doctor"] and len(words) == 1:
            return doctor_text(resolve_config(host))
        return "Usage: /engagements doctor"
    except Exception:  # noqa: BLE001
        logger.debug("engagements /engagements failed", exc_info=True)
        return "Engagements could not complete that command."


# --- CLI (spec 6.3): hermes engagements init|list|show|doctor -----------------------------------


def _cli_init(host, root, set_setting):
    if not isinstance(root, str) or not root.strip():
        return 2, "init needs --root <path>."
    path = os.path.abspath(os.path.expanduser(root.strip()))
    try:
        library = open_library(path, create_default=True)
    except EngagementsError as exc:
        return 1, f"Could not create or join a library at {path}: {exc.message} ({exc.code})"
    if set_setting is None:
        return 1, (f"Library ready at {library.root} (id {library.library_id}), but this Hermes cannot store the "
                   f"setting. Set plugins.entries.engagements.settings.library_root to {library.root}.")
    try:
        set_setting("library_root", os.fspath(library.root))
    except Exception:  # noqa: BLE001
        return 1, (f"Library ready at {library.root}, but the setting could not be saved. Set "
                   f"plugins.entries.engagements.settings.library_root to {library.root}.")
    verb = "Created" if library.created else "Joined"
    return 0, f"{verb} the library at {library.root} (id {library.library_id}) and saved it as library_root."


def _cli_show(session, record_id):
    if not isinstance(record_id, str) or not record_id.strip():
        labels = getattr(session.config, "labels", None)
        client_word, _ = binding.display_words(labels, article=True)  # the default keeps its article
        _, project_word = binding.display_words(labels)
        return 2, f"show needs {client_word} or {project_word} id."
    result = call_tool("engagements_context", {"id": record_id.strip()}, session)
    if not result["ok"]:
        return 1, _error_text(result["error"])
    data = result["data"]
    properties = data.get("properties") or {}
    lines = [f"{properties.get('name') or data.get('id')} ({data.get('id')}, {data.get('kind')})",
             f"path: {data.get('path')}   rev: {data.get('rev')}"]
    lines.extend(f"{key}: {value}" for key, value in properties.items() if key not in ("engagements", "id", "name"))
    for section in data.get("sections") or ():
        lines.extend(["", f"## {section.get('title')}", (section.get("text") or "").strip()])
    if data.get("next_offset") is not None:
        lines.extend(["", f"(more text follows; this is the first page, next_offset {data['next_offset']})"])
    lines.extend(f"warning {w.get('code')}: {w.get('message')}" for w in result.get("warnings") or ())
    return 0, "\n".join(lines)


def cli_run(host, args, set_setting=None):
    """Run one ``hermes engagements`` subcommand. Returns ``(exit_code, text)``; the caller prints."""
    command = getattr(args, "engagements_command", None)
    try:
        config = _quiet(resolve_config(host))
        if command == "init":
            return _cli_init(host, getattr(args, "root", None), set_setting)
        session = build_session(host, None, config)
        if command == "list":
            ok, text = list_result(session, config.labels)
            return (0 if ok else 1), text
        if command == "show":
            return _cli_show(session, getattr(args, "id", None))
        if command == "doctor":
            if config.library_root is None:
                return 1, doctor_text(config)
            report = doctor.run_doctor(config.library_root)
            return (0 if report.clean else 1), report.render_text()
        return 2, "Usage: hermes engagements init --root <path> | list | show <id> | doctor"
    except Exception:  # noqa: BLE001
        logger.debug("engagements CLI failed", exc_info=True)
        return 1, "Engagements could not complete that command."


__all__ = ["Host", "PluginConfig", "banner_hook", "build_session", "client_command", "cli_run", "compaction_hook",
           "delete_hook", "engagements_command", "render_state", "reset_hook", "resolve_config", "run_tool",
           "state_key"]
