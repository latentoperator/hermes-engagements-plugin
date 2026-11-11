"""Every ``hermes engagements`` subcommand must exit nonzero on failure.

``list`` used to return exit 0 after rendering "No library exists yet", because the
rendered text was returned without the failed lookup's status.
"""

import argparse

import pytest
from plugin_support import registered
from tools_support import copy_library


def parser_for(ctx):
    root = argparse.ArgumentParser(prog="hermes")
    sub = root.add_subparsers(dest="command")
    entry = ctx.cli["engagements"]
    entry["setup_fn"](sub.add_parser("engagements", help=entry["help"]))
    return root


def run(ctx, capsys, *argv):
    args = parser_for(ctx).parse_args(["engagements", *argv])
    code = ctx.cli["engagements"]["handler_fn"](args)
    return code, capsys.readouterr().out


def unusable_root(tmp_path, kind):
    """A configured ``library_root`` that cannot be opened, in three forms."""
    if kind == "missing":
        return tmp_path / "absent"
    if kind == "file":
        path = tmp_path / "as-a-file"
        path.write_text("not a directory", encoding="utf-8")
        return path
    root = copy_library(tmp_path)
    (root / ".engagements" / "library.yaml").write_text("format: 99\nlibrary_id: nope\n", encoding="utf-8")
    return root


@pytest.mark.parametrize("kind", ["missing", "file", "corrupt"])
@pytest.mark.parametrize("argv", [("list",), ("show", "acme-bakery"), ("doctor",)], ids=["list", "show", "doctor"])
def test_every_cli_subcommand_exits_nonzero_when_the_library_cannot_open(monkeypatch, tmp_path, capsys, kind, argv):
    _, ctx = registered(monkeypatch, tmp_path, library=unusable_root(tmp_path, kind))
    code, out = run(ctx, capsys, *argv)
    assert code == 1
    assert out.strip() and "Traceback" not in out


def test_cli_list_still_exits_zero_for_a_healthy_library(monkeypatch, tmp_path, capsys):
    root = copy_library(tmp_path)
    _, ctx = registered(monkeypatch, tmp_path, library=root)
    code, out = run(ctx, capsys, "list")
    assert code == 0 and "Acme Bakery (acme-bakery)" in out


def test_init_keeps_its_nonzero_paths(monkeypatch, tmp_path, capsys):
    _, ctx = registered(monkeypatch, tmp_path)
    assert run(ctx, capsys, "init", "--root", "  ")[0] == 2
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    assert run(ctx, capsys, "init", "--root", str(blocker))[0] == 1
