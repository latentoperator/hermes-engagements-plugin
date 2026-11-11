"""hermes engagements init|list|show|doctor: the argparse wiring and the handlers behind it."""

import argparse

import pytest
from plugin_support import registered
from tools_support import copy_library


def parser_for(ctx):
    """The parser Hermes builds: the plugin's setup_fn fills the ``engagements`` subparser."""
    root = argparse.ArgumentParser(prog="hermes")
    sub = root.add_subparsers(dest="command")
    entry = ctx.cli["engagements"]
    entry["setup_fn"](sub.add_parser("engagements", help=entry["help"]))
    return root


def run(ctx, capsys, *argv):
    args = parser_for(ctx).parse_args(["engagements", *argv])
    code = ctx.cli["engagements"]["handler_fn"](args)
    return code, capsys.readouterr().out


def setup(monkeypatch, tmp_path, **kwargs):
    root = copy_library(tmp_path)
    plugin, ctx = registered(monkeypatch, tmp_path, library=root, **kwargs)
    return root, ctx


def test_the_parser_accepts_each_subcommand_and_requires_the_arguments(monkeypatch, tmp_path):
    _, ctx = setup(monkeypatch, tmp_path)
    parser = parser_for(ctx)
    assert parser.parse_args(["engagements", "list"]).engagements_command == "list"
    assert parser.parse_args(["engagements", "doctor"]).engagements_command == "doctor"
    assert parser.parse_args(["engagements", "show", "acme-bakery"]).id == "acme-bakery"
    assert parser.parse_args(["engagements", "init", "--root", "/x"]).root == "/x"
    for bad in (["engagements", "init"], ["engagements", "show"]):
        with pytest.raises(SystemExit):
            parser.parse_args(bad)


def test_list_prints_clients_and_projects_and_exits_zero(monkeypatch, tmp_path, capsys):
    _, ctx = setup(monkeypatch, tmp_path)
    code, out = run(ctx, capsys, "list")
    assert code == 0 and "Acme Bakery (acme-bakery)" in out and "Website 2026" in out


def test_show_prints_a_client_and_a_project(monkeypatch, tmp_path, capsys):
    _, ctx = setup(monkeypatch, tmp_path)
    code, out = run(ctx, capsys, "show", "acme-bakery")
    assert code == 0 and out.startswith("Acme Bakery (acme-bakery, client)") and "## " in out
    code, out = run(ctx, capsys, "show", "acme-website-2026")
    assert code == 0 and "locations:" in out and "~/clients/acme/website-2026" in out


def test_show_an_unknown_id_exits_one_with_a_sentence(monkeypatch, tmp_path, capsys):
    _, ctx = setup(monkeypatch, tmp_path)
    code, out = run(ctx, capsys, "show", "nobody")
    assert code == 1 and "nobody" in out and "Traceback" not in out


def test_doctor_exits_zero_on_a_clean_library_and_one_on_a_problem(monkeypatch, tmp_path, capsys):
    root, ctx = setup(monkeypatch, tmp_path)
    code, out = run(ctx, capsys, "doctor")
    assert code == 0 and out.startswith("Engagements doctor:")
    record = root / "Acme Bakery" / "Client.md"
    record.write_text(record.read_text(encoding="utf-8") + "\npassword: hunter2hunter2hunter2\n", encoding="utf-8")
    code, out = run(ctx, capsys, "doctor")
    assert code == 1 and "hunter2hunter2hunter2" not in out


def test_doctor_with_no_configured_location_exits_one(monkeypatch, tmp_path, capsys):
    _, ctx = registered(monkeypatch, tmp_path, settings={"library_root": "relative"})
    code, out = run(ctx, capsys, "doctor")
    assert code == 1 and "No library location is configured" in out


def test_list_and_show_and_doctor_never_create_a_library(monkeypatch, tmp_path, capsys):
    _, ctx = registered(monkeypatch, tmp_path)
    for argv in (("list",), ("show", "x"), ("doctor",)):
        code, out = run(ctx, capsys, *argv)
        assert code == 1  # every subcommand fails on a library that does not exist
    assert not (tmp_path / "hermes-home" / "plugin-data" / "engagements" / "library").exists()


# --- init ---------------------------------------------------------------------------------------


def test_init_creates_the_library_and_stores_library_root(monkeypatch, tmp_path, capsys):
    _, ctx = registered(monkeypatch, tmp_path)
    target = tmp_path / "studio-library"
    code, out = run(ctx, capsys, "init", "--root", str(target))
    assert code == 0 and out.startswith("Created the library at ")
    assert (target / ".engagements" / "library.yaml").is_file()
    assert ctx.saved == {"library_root": str(target.resolve())}
    # The saved setting is what the tools now use.
    found = ctx.call_tool("engagements_find", {"query": ""}, session_id="s1")
    assert found["ok"] is True and found["warnings"] == []


def test_init_on_an_existing_library_joins_it_without_changing_records(monkeypatch, tmp_path, capsys):
    root = copy_library(tmp_path)
    _, ctx = registered(monkeypatch, tmp_path)
    before = sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())
    code, out = run(ctx, capsys, "init", "--root", str(root))
    assert code == 0 and out.startswith("Joined the library at ")
    assert sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()) == before


def test_init_expands_a_tilde_and_absolutizes_a_relative_path(monkeypatch, tmp_path, capsys):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)
    _, ctx = registered(monkeypatch, tmp_path)
    assert run(ctx, capsys, "init", "--root", "~/lib")[0] == 0
    assert (home / "lib" / ".engagements" / "library.yaml").is_file()
    assert run(ctx, capsys, "init", "--root", "rel-lib")[0] == 0
    assert ctx.saved["library_root"] == str((tmp_path / "rel-lib").resolve())


def test_init_refuses_a_blank_root_and_a_file_in_the_way(monkeypatch, tmp_path, capsys):
    _, ctx = registered(monkeypatch, tmp_path)
    assert run(ctx, capsys, "init", "--root", "  ")[0] == 2
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    code, out = run(ctx, capsys, "init", "--root", str(blocker))
    assert code == 1 and "Traceback" not in out and ctx.saved == {}


def _ctx_with_writer(monkeypatch, tmp_path, writer):
    plugin, ctx = registered(monkeypatch, tmp_path)
    other = type(ctx)()
    other.set_config = writer  # None models a Hermes without a settings writer
    plugin.register(other)
    return other


def test_init_tells_the_user_how_to_save_the_setting_when_the_host_cannot(monkeypatch, tmp_path, capsys):
    ctx = _ctx_with_writer(monkeypatch, tmp_path, None)
    code, out = run(ctx, capsys, "init", "--root", str(tmp_path / "lib"))
    assert code == 1 and "plugins.entries.engagements.settings.library_root" in out
    assert (tmp_path / "lib" / ".engagements" / "library.yaml").is_file()


def test_init_reports_a_settings_write_failure_without_a_traceback(monkeypatch, tmp_path, capsys):
    def broken(key, value):
        raise PermissionError("/secret/config.yaml")

    ctx = _ctx_with_writer(monkeypatch, tmp_path, broken)
    code, out = run(ctx, capsys, "init", "--root", str(tmp_path / "lib"))
    assert code == 1 and "could not be saved" in out and "secret" not in out and "Traceback" not in out


def test_no_subcommand_prints_usage_and_exits_two(monkeypatch, tmp_path, capsys):
    _, ctx = registered(monkeypatch, tmp_path)
    args = parser_for(ctx).parse_args(["engagements"])
    code = ctx.cli["engagements"]["handler_fn"](args)
    assert code == 2 and "Usage: hermes engagements" in capsys.readouterr().out
