"""engprobe: Phase 0 runtime probe for Hermes Engagements (throwaway, never shipped).

Every seam appends one JSON line to the evidence log so RUNTIME-EVIDENCE.md can cite
runtime output, not source reading. Public plugin APIs only.
"""

from __future__ import annotations

import fcntl
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

_CTX: Any = None
_LOCK = threading.Lock()


def _log_path() -> Path:
    configured = _CTX.get_config("log_path", default="") if _CTX else ""
    if configured:
        return Path(configured)
    return _CTX.state.data_dir / "probe.jsonl"


def _session_env() -> dict:
    try:
        from gateway.session_context import get_session_env
    except Exception as exc:  # pragma: no cover - probe only
        return {"error": repr(exc)}
    return {k: get_session_env(k, "") for k in (
        "HERMES_SESSION_ID", "HERMES_SESSION_KEY", "HERMES_SESSION_PLATFORM", "HERMES_SESSION_PROFILE")}


def _common() -> dict:
    out = {"ts": time.time(), "pid": os.getpid(), "thread": threading.current_thread().name,
           "hermes_home": os.environ.get("HERMES_HOME", ""), "profile": _CTX.profile_name,
           "os_getcwd": os.getcwd()}
    for name in ("session_cwd", "session_surface"):
        try:
            out[name] = getattr(_CTX, name)()
        except Exception as exc:
            out[name] = f"ERR {exc!r}"
    try:
        from agent.runtime_cwd import resolve_agent_cwd
        out["resolve_agent_cwd"] = str(resolve_agent_cwd())
    except Exception as exc:
        out["resolve_agent_cwd"] = f"ERR {exc!r}"
    try:
        out["data_dir"] = str(_CTX.state.data_dir)
        from plugins.plugin_storage import plugin_data_dir
        out["plugin_data_dir"] = str(plugin_data_dir("engprobe"))
    except Exception as exc:
        out["data_dir"] = f"ERR {exc!r}"
    out["session_env"] = _session_env()
    return out


def _emit(seam: str, **fields) -> dict:
    rec = {"seam": seam, **_common(), **fields}
    path = _log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK, open(path, "a", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(json.dumps(rec, default=str) + "\n")
    return rec


def _ids(kwargs: dict) -> dict:
    keep = ("session_id", "task_id", "session_key", "platform", "old_session_id", "kind", "reason",
            "is_first_turn", "model", "completed", "interrupted", "turn_id", "parent_session_id",
            "new_session_id")
    return {"kw_keys": sorted(kwargs), **{k: kwargs.get(k) for k in keep if k in kwargs}}


# ---- bindings (S4/S5): keyed by session id in profile-scoped plugin state -------------------

def _bindings() -> dict:
    return _CTX.state.get("bindings", default={}) or {}


def _set_binding(sid: str, client: str | None) -> None:
    b = _bindings()
    if client:
        b[sid] = client
    else:
        b.pop(sid, None)
    _CTX.state.set("bindings", b)


# ---- tools ----------------------------------------------------------------------------------

def _where(args: dict, **kwargs) -> str:
    sleep_s = float(args.get("sleep_s") or 0)
    start = _emit("S1.tool.start", label=args.get("label", ""), **_ids(kwargs))
    if sleep_s:
        time.sleep(sleep_s)
        end = _emit("S1.tool.end", label=args.get("label", ""), **_ids(kwargs))
        start = {**start, "after_sleep_session_cwd": end.get("session_cwd"),
                 "after_sleep_session_id": kwargs.get("session_id")}
    sid = kwargs.get("session_id") or ""
    return json.dumps({"ok": True, "label": args.get("label", ""), "session_id": sid,
                       "task_id": kwargs.get("task_id"), "session_cwd": start.get("session_cwd"),
                       "surface": start.get("session_surface"), "pid": start["pid"],
                       "bound": _bindings().get(sid), "data_dir": start.get("data_dir")})


def _bind(args: dict, **kwargs) -> str:
    sid = kwargs.get("session_id") or ""
    client = args.get("client") or None
    if not sid:
        _emit("S4.bind.no_session", **_ids(kwargs))
        return json.dumps({"ok": False, "error": "no session_id in handler kwargs"})
    _set_binding(sid, client)
    _emit("S4.bind", client=client, **_ids(kwargs))
    return json.dumps({"ok": True, "session_id": sid, "client": client})


def _lock(args: dict, **kwargs) -> str:
    root = Path(args.get("root") or _CTX.get_config("library_root", default="") or _CTX.state.data_dir)
    hold = float(args.get("hold_s") or 0)
    root.mkdir(parents=True, exist_ok=True)
    marker = root / "shared-marker.txt"
    t0 = time.time()
    with open(root / "probe.lock", "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        waited = time.time() - t0
        before = marker.read_text() if marker.exists() else ""
        marker.write_text(before + f"{_CTX.profile_name} pid={os.getpid()} t={time.time():.3f}\n")
        time.sleep(hold)
        fcntl.flock(fh, fcntl.LOCK_UN)
    _emit("S6.lock", root=str(root), waited_s=round(waited, 3), hold_s=hold, seen_before=before,
          **_ids(kwargs))
    return json.dumps({"ok": True, "waited_s": round(waited, 3), "seen_before": before})


_SCHEMAS = {
    "engprobe_where": {"name": "engprobe_where", "description": "Phase 0 probe: report session identity and cwd.",
                       "parameters": {"type": "object", "properties": {
                           "label": {"type": "string"}, "sleep_s": {"type": "number"}}}},
    "engprobe_bind": {"name": "engprobe_bind", "description": "Phase 0 probe: bind this session to a fictional client.",
                      "parameters": {"type": "object", "properties": {"client": {"type": "string"}}}},
    "engprobe_lock": {"name": "engprobe_lock", "description": "Phase 0 probe: take the shared library lock.",
                      "parameters": {"type": "object", "properties": {
                          "root": {"type": "string"}, "hold_s": {"type": "number"}}}},
}


# ---- hooks ----------------------------------------------------------------------------------

def _pre_llm_call(**kwargs):
    sid = kwargs.get("session_id") or ""
    bound = _bindings().get(sid)
    _emit("S2.pre_llm_call", bound=bound, history_len=len(kwargs.get("conversation_history") or []),
          **_ids(kwargs))
    return {"context": f"[ENGPROBE-BANNER sid={sid} bound={bound}]"}


def _post_compaction(**kwargs):
    old, new = kwargs.get("old_session_id") or "", kwargs.get("session_id") or ""
    b = _bindings()
    migrated = None
    if old and new and old in b:
        migrated = b[old]
        b[new] = migrated
        _CTX.state.set("bindings", b)
    _emit("S4.post_compaction", migrated=migrated, **_ids(kwargs))


def _observer(name):
    def cb(**kwargs):
        _emit(f"S4.{name}", **_ids(kwargs))
    return cb


# ---- slash + CLI ----------------------------------------------------------------------------

def _slash(raw_args: str, **kwargs) -> str:
    rec = _emit("S3.slash", raw_args=raw_args, **_ids(kwargs))
    parts = raw_args.split()
    sid = kwargs.get("session_id") or ""
    if parts[:1] == ["bind"] and sid:
        _set_binding(sid, parts[1] if len(parts) > 1 else None)
    return (f"engprobe slash: session_id={sid!r} session_key={kwargs.get('session_key')!r} "
            f"platform={kwargs.get('platform')!r} cwd={rec.get('session_cwd')!r} "
            f"bound={_bindings().get(sid)!r}")


def _cli_setup(sub):
    subs = sub.add_subparsers(dest="engprobe_cmd")
    subs.add_parser("show", help="Show probe state")
    sub.set_defaults(func=_cli_handler)


def _cli_handler(args):
    print(json.dumps({"data_dir": str(_CTX.state.data_dir), "bindings": _bindings(),
                      "log": str(_log_path()), "profile": _CTX.profile_name}, indent=2))


def register(ctx):
    global _CTX
    _CTX = ctx
    handlers = {"engprobe_where": _where, "engprobe_bind": _bind, "engprobe_lock": _lock}
    for name, fn in handlers.items():
        ctx.register_tool(name=name, toolset="engprobe", schema=_SCHEMAS[name], handler=fn)
    ctx.register_hook("pre_llm_call", _pre_llm_call)
    ctx.register_hook("post_compaction", _post_compaction)
    for name in ("on_session_start", "on_session_end", "on_session_reset", "on_session_finalize"):
        ctx.register_hook(name, _observer(name))
    ctx.register_command("engprobe", _slash, description="Phase 0 probe", args_hint="[bind <client>]")
    ctx.register_cli_command(name="engprobe", help="Phase 0 probe", setup_fn=_cli_setup,
                             handler_fn=_cli_handler)
    skill = Path(__file__).parent / "skills" / "probe-skill" / "SKILL.md"
    ctx.register_skill("probe-skill", skill)
