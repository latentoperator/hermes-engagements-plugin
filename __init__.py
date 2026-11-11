"""Hermes Engagements: a maintained, file-based client foundation for Hermes agents.

This module is the only place the plugin touches Hermes. ``register(ctx)`` builds a
``Host`` of plain callables from the plugin context (public APIs only, spec D9) and
hands every tool, hook, slash command and CLI action to ``engagements.runtime``,
which keeps no process-global session state (spec A1). Nothing here is shared between
sessions or profiles: each callback asks ``ctx`` for its own session, cwd, surface,
settings and state.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .engagements import runtime
from .engagements.tools import TOOL_NAMES, TOOL_SCHEMAS

TOOLSET = "engagements"
PLUGIN_DATA_NAME = "engagements"
# Registered only when the skill file exists, so a checkout without skills/ still loads.
# A8: a description is passed explicitly, or the skill is listed without one.
SKILLS = {
    "onboard-client": "Add a client: draft a short record from the user's answers or pasted notes, "
                      "correct it with them, then save it with engagements_create.",
    "client-work": "Work for a client: bind the session, read the record, keep project locations current "
                   "and make targeted, sourced updates with engagements_update.",
    "suggest-clients": "Suggest clients: review past chats for clients or recurring projects, present up to "
                       "five as cited guesses, and create nothing until the user picks one.",
}
HOOKS = ("pre_llm_call", "post_compaction", "on_session_reset", "on_session_delete")


def _default_root():
    """``<HERMES_HOME>/plugin-data/engagements/library`` for the active profile (spec 6.1, A7)."""
    # Public plugin API, imported when asked so the module loads without Hermes (tests).
    from plugins.plugin_storage import plugin_data_dir

    return plugin_data_dir(PLUGIN_DATA_NAME) / "library"


def _now():
    return datetime.now().astimezone()


def _changed(what):
    """Push ``plugin.engagements.changed`` to connected Desktop windows (public event API)."""
    from hermes_cli.plugin_events import broadcast_plugin_event

    broadcast_plugin_event(TOOLSET, "changed", {"what": what})


def _host(ctx):
    return runtime.Host(
        get_setting=ctx.get_config,
        default_root=_default_root,
        state_get=lambda key: ctx.state.get(key),
        state_set=lambda key, value: ctx.state.set(key, value),
        session_cwd=ctx.session_cwd,
        session_surface=ctx.session_surface,
        profile=lambda: ctx.profile_name,
        clock=_now,
        changed=_changed,
    )


def _tool(host, name):
    def handler(args, **kwargs):
        return runtime.run_tool(host, name, args, kwargs.get("session_id"))

    handler.__name__ = name
    return handler


def _hook(host, function):
    def callback(**kwargs):
        return function(host, **kwargs)

    callback.__name__ = function.__name__
    return callback


def _cli_setup(subparser):
    commands = subparser.add_subparsers(dest="engagements_command")
    init = commands.add_parser("init", help="Create or join a library and store it as library_root")
    init.add_argument("--root", required=True, help="Library folder (created if missing)")
    commands.add_parser("list", help="List active clients and projects")
    show = commands.add_parser("show", help="Show one client or project record")
    show.add_argument("id", help="Client or project id")
    commands.add_parser("doctor", help="Validate the library")


def register(ctx):
    host = _host(ctx)
    for name in TOOL_NAMES:
        ctx.register_tool(name=name, toolset=TOOLSET, schema=TOOL_SCHEMAS[name], handler=_tool(host, name))
    ctx.register_hook("pre_llm_call", _hook(host, runtime.banner_hook))
    ctx.register_hook("post_compaction", _hook(host, runtime.compaction_hook))
    ctx.register_hook("on_session_reset", _hook(host, runtime.reset_hook))
    ctx.register_hook("on_session_delete", _hook(host, runtime.delete_hook))

    def client(raw_args, session_id=None, **kwargs):
        return runtime.client_command(host, raw_args, session_id)

    def engagements(raw_args, **kwargs):
        return runtime.engagements_command(host, raw_args)

    ctx.register_command("client", client, description="Show or change the client this session is bound to",
                         args_hint="[<name-or-id> | off | auto | list]")
    ctx.register_command("engagements", engagements, description="Engagements maintenance",
                         args_hint="doctor")

    set_setting = getattr(ctx, "set_config", None)

    def run_cli(args):
        code, text = runtime.cli_run(host, args, set_setting)
        print(text)
        return code

    ctx.register_cli_command(name="engagements", help="Manage the Engagements client library",
                             setup_fn=_cli_setup, handler_fn=run_cli)
    skills = Path(__file__).parent / "skills"
    for name, description in SKILLS.items():
        path = skills / name / "SKILL.md"
        if path.is_file():
            ctx.register_skill(name, path, description=description)
