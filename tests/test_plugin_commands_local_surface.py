"""SPEC 6.4: a slash command's own surface value is not proof of CLI, TUI or Desktop.

Phases 3 and 3D observed ``local`` inside a slash handler on the classic CLI, the TUI and Desktop alike.
Treating ``local`` as ``cli`` would let any local-source command resolve cwd and write automatic state.
``local`` therefore stays unsupported for cwd resolution on its own: the status is read-only there, and only
a surface in the verified set maintains the binding it reports. ``test_plugin_desktop_surface.py`` covers the
one way a ``local`` command may act: on the turn-verified surface of its own session id.
"""

import pytest
from plugin_support import project_home, registered
from tools_support import binding_key_for, copy_library

UNAVAILABLE = "binding from the folder is not available on this surface."


def setup(monkeypatch, tmp_path, **kwargs):
    root = copy_library(tmp_path)
    return root, registered(monkeypatch, tmp_path, library=root, **kwargs)


def client(ctx, raw=""):
    return ctx.command("client", raw, session_id="s1", session_key="key", platform="local")


def stored(ctx, root):
    return ctx.state.values.get(binding_key_for(root, "s1"))


@pytest.mark.parametrize("surface", ["local", "acp", "", "unknown-surface", None])
def test_an_unverified_surface_never_auto_binds_or_writes_automatic_state(monkeypatch, tmp_path, surface):
    """``local`` with no turn behind it and every unrecognized value (including none) stay unsupported."""
    home = project_home(monkeypatch, tmp_path)
    root, (_, ctx) = setup(monkeypatch, tmp_path, cwd=str(home / "clients" / "riverbend" / "recall"),
                           surface=surface)
    text = client(ctx)
    assert UNAVAILABLE in text
    assert "Bound" not in text and "bound (auto)" not in text
    assert "riverbend-recall-campaign" not in text  # the folder was never resolved
    assert stored(ctx, root) is None
    assert ctx.state.writes == []  # no automatic binding state, and nothing else either


@pytest.mark.parametrize("surface", ["cli", "tui", "desktop"])
def test_a_genuine_cli_tui_or_desktop_surface_still_binds_from_the_folder(monkeypatch, tmp_path, surface):
    """The control: the verified surfaces keep their SPEC 6.4 behaviour."""
    home = project_home(monkeypatch, tmp_path)
    root, (_, ctx) = setup(monkeypatch, tmp_path, cwd=str(home / "clients" / "riverbend" / "recall"),
                           surface=surface)
    text = client(ctx)
    assert "Project: Recall Campaign (riverbend-recall-campaign)" in text
    assert "does not use the working directory" not in text
    assert stored(ctx, root) == {"mode": "auto", "client": "riverbend-dental",
                                 "project": "riverbend-recall-campaign"}


def test_a_stored_automatic_binding_is_reported_read_only_and_not_rewritten(monkeypatch, tmp_path):
    """A cli, tui or desktop turn already stored the identity; the read-only status names it and writes nothing."""
    home = project_home(monkeypatch, tmp_path)
    root, (_, ctx) = setup(monkeypatch, tmp_path, cwd=str(home / "clients" / "riverbend" / "recall"),
                           surface="local")
    before = {"mode": "auto", "client": "riverbend-dental", "project": "riverbend-recall-campaign"}
    ctx.state.values[binding_key_for(root, "s1")] = dict(before)
    text = client(ctx)
    assert UNAVAILABLE in text
    assert "Last stored automatic binding: client riverbend-dental, project riverbend-recall-campaign." in text
    assert "Bound" not in text
    assert stored(ctx, root) == before and ctx.state.writes == []


def test_explicit_bind_still_works_on_a_local_command(monkeypatch, tmp_path):
    """SPEC 6.4 allows explicit modes on any surface; only cwd resolution is withheld."""
    root, (_, ctx) = setup(monkeypatch, tmp_path, surface="local")
    text = client(ctx, "acme-bakery")
    assert text.startswith("Binding updated.")
    assert "Bound (stored). Client: Acme Bakery (acme-bakery)" in text
    assert UNAVAILABLE in text
    assert stored(ctx, root)["mode"] == "explicit" and stored(ctx, root)["client"] == "acme-bakery"


@pytest.mark.parametrize("word", ["off", "auto"])
def test_off_and_auto_still_work_on_a_local_command(monkeypatch, tmp_path, word):
    """``/client off`` and ``/client auto`` keep working; auto does not resolve from this surface."""
    home = project_home(monkeypatch, tmp_path)
    root, (_, ctx) = setup(monkeypatch, tmp_path, cwd=str(home / "clients" / "riverbend" / "recall"),
                           surface="local")
    text = client(ctx, word)
    assert text.startswith("Binding updated.")
    assert UNAVAILABLE in text
    assert stored(ctx, root) == {"mode": word}
    # The only state write is the mode change; no automatic identity was added.
    assert [value for _, value in ctx.state.writes] == [{"mode": word}]
