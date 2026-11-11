"""Shared helpers for the plugin-wiring tests: a fake Hermes ``ctx`` and the plugin package loader.

The fake covers exactly the public API surface the root ``__init__.py`` calls
(``register_tool``, ``register_hook``, ``register_command``, ``register_cli_command``,
``register_skill``, ``get_config``, ``set_config``, ``state``, ``session_cwd``,
``session_surface``, ``profile_name``) and enforces the same state-key and JSON rules
Hermes enforces, so a wrong key or value fails here and not only in a runtime check.
Fictional data only.
"""

import importlib.util
import json
import re
import sys
import types
from pathlib import Path

from tools_support import fixed_clock

ROOT = Path(__file__).resolve().parent.parent
_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


def load_plugin(name="hermes_plugins_engagements_test", root=ROOT):
    """Import the repository root as a package, the way Hermes loads a plugin directory."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, root / "__init__.py", submodule_search_locations=[str(root)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def install_plugin_storage(monkeypatch, data_dir):
    """A stand-in for ``plugins.plugin_storage`` that records the plugin name it was asked for."""
    calls = []

    def plugin_data_dir(name):
        calls.append(name)
        path = Path(data_dir) / "plugin-data" / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    package = types.ModuleType("plugins")
    package.__path__ = []
    storage = types.ModuleType("plugins.plugin_storage")
    storage.plugin_data_dir = plugin_data_dir
    package.plugin_storage = storage
    monkeypatch.setitem(sys.modules, "plugins", package)
    monkeypatch.setitem(sys.modules, "plugins.plugin_storage", storage)
    return calls


class FakeState:
    """``ctx.state``: JSON values under validated keys, ``get``/``set`` only (no delete)."""

    def __init__(self):
        self.values = {}
        self.writes = []
        self.fail = None

    def _check(self, key):
        if not isinstance(key, str) or not _KEY.fullmatch(key) or ".." in key:
            raise ValueError(f"Plugin state keys must be 1-128 characters: {key!r}")
        if self.fail is not None:
            raise self.fail

    def get(self, key, default=None):
        self._check(key)
        return json.loads(json.dumps(self.values[key])) if key in self.values else default

    def set(self, key, value):
        self._check(key)
        self.values[key] = json.loads(json.dumps(value))
        self.writes.append((key, value))


class FakeCtx:
    """The slice of ``PluginContext`` the plugin uses, recording every registration."""

    def __init__(self, settings=None, cwd="", surface="cli", profile="studio-dev"):
        self.settings = {} if settings is None else settings
        self.cwd = cwd
        self.surface = surface
        self.profile_name = profile
        self.config_fail = None  # set to an exception to model an unreadable settings store
        self.state = FakeState()
        self.tools = {}
        self.hooks = {}
        self.commands = {}
        self.cli = {}
        self.skills = {}
        self.saved = {}

    def get_config(self, key, default=None):
        if self.config_fail is not None:
            raise self.config_fail
        value = self.settings
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value

    def set_config(self, key, value):
        self.saved[key] = value
        self.settings[key] = value

    def session_cwd(self):
        return self.cwd

    def session_surface(self):
        return self.surface

    def register_tool(self, name, toolset, schema, handler, **kwargs):
        self.tools[name] = {"toolset": toolset, "schema": schema, "handler": handler, **kwargs}

    def register_hook(self, hook_name, callback, **kwargs):
        self.hooks[hook_name] = callback

    def register_command(self, name, handler, description="", args_hint="", **kwargs):
        self.commands[name] = {"handler": handler, "description": description, "args_hint": args_hint}

    def register_cli_command(self, name, help, setup_fn, handler_fn=None, description=""):  # noqa: A002 - Hermes' name
        self.cli[name] = {"help": help, "setup_fn": setup_fn, "handler_fn": handler_fn}

    def register_skill(self, name, path, description="", frontmatter=None):
        self.skills[name] = {"path": Path(path), "description": description}

    # --- driving the registered callbacks the way Hermes does -------------------------

    def call_tool(self, name, args, **kwargs):
        return json.loads(self.tools[name]["handler"](args, **kwargs))

    def hook(self, name, **kwargs):
        return self.hooks[name](**kwargs)

    def command(self, name, raw_args="", **kwargs):
        return self.commands[name]["handler"](raw_args, **kwargs)


def registered(monkeypatch, tmp_path, *, settings=None, cwd="", surface="cli", library=None, clock=fixed_clock):
    """Register the plugin on a fresh fake ctx whose default root lives under ``tmp_path``.

    The host clock is pinned (``tools_support.fixed_clock`` by default) before registration, so no
    plugin test depends on the wall clock.
    """
    install_plugin_storage(monkeypatch, tmp_path / "hermes-home")
    settings = dict(settings or {})
    if library is not None:
        settings["library_root"] = str(library)
    ctx = FakeCtx(settings, cwd, surface)
    plugin = load_plugin()
    monkeypatch.setattr(plugin, "_now", clock)
    plugin.register(ctx)
    return plugin, ctx


def project_home(monkeypatch, tmp_path):
    """A fake home holding the fixture library's project folders; ``~`` resolves into it."""
    home = tmp_path / "home"
    for relative in ("clients/shared", "clients/acme/website-2026", "clients/riverbend/recall"):
        (home / relative).mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    return home.resolve()
