"""An unreadable ``library_root`` must fail closed.

Every ``get_config`` exception used to become a default, so a create ran against the default
library even when the profile had configured another one.
"""

import json

from plugin_support import registered
from tools_support import copy_library


def setup(monkeypatch, tmp_path):
    root = copy_library(tmp_path)
    plugin, ctx = registered(monkeypatch, tmp_path, library=root)
    return root, plugin, ctx


def default_library(tmp_path):
    return tmp_path / "hermes-home" / "plugin-data" / "engagements" / "library"


def test_create_fails_closed_when_the_library_setting_cannot_be_read(monkeypatch, tmp_path):
    _, _, ctx = setup(monkeypatch, tmp_path)
    ctx.config_fail = RuntimeError("Cannot read plugin config /secret/config.yaml")
    result = ctx.call_tool("engagements_create", {"kind": "client", "name": "Acme Bakery"}, session_id="s1")
    assert result["ok"] is False
    assert result["error"]["code"] == "library_missing"
    assert result["error"]["details"]["reason"] == "no_library_root"
    assert not default_library(tmp_path).exists()  # no silent redirect into the default library
    assert "secret" not in json.dumps(result) and "Traceback" not in json.dumps(result)


def test_commands_and_cli_report_an_unreadable_setting_instead_of_using_the_default(monkeypatch, tmp_path, capsys):
    _, _, ctx = setup(monkeypatch, tmp_path)
    ctx.config_fail = RuntimeError("Cannot read plugin config")
    assert "No library location is configured" in ctx.command("client", "list")
    args = type("Args", (), {"engagements_command": "list"})()
    code = ctx.cli["engagements"]["handler_fn"](args)
    out = capsys.readouterr().out
    assert code == 1 and "No library location is configured" in out
    assert not default_library(tmp_path).exists()
