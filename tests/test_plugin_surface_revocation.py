"""An unsupported turn revokes the stored surface even when the library is unavailable.

The turn-verified surface (``surface:<session_id>``) is what lets ``/client`` treat the ambiguous ``local``
value as ``cli``, ``tui`` or ``desktop``. Revoking it must not depend on the library opening, or a record
written on a supported turn survives an unsupported one and authorizes cwd binding again once the library
comes back. Every test drives the registered hooks and commands. Fictional data only.
"""

import os
import shutil

import pytest
from plugin_support import project_home, registered
from tools_support import FIXTURES, binding_key_for, copy_library

UNAVAILABLE = "binding from the folder is not available on this surface."
UNSUPPORTED = ["local", "acp", "telegram", ""]


def surface_key(session_id="s1"):
    return f"surface:{session_id}"


def turn(ctx, session_id="s1"):
    return ctx.hook("pre_llm_call", session_id=session_id, user_message="hi", conversation_history=[],
                    is_first_turn=False, model="stub", platform=ctx.surface)


def client(ctx, raw="", session_id="s1"):
    """A slash command as Hermes delivers it: the command path reports ``local`` whatever the surface was."""
    surface, ctx.surface = ctx.surface, "local"
    try:
        return ctx.command("client", raw, session_id=session_id, session_key=f"key-{session_id}", platform="local")
    finally:
        ctx.surface = surface


def make_unavailable(kind, root, ctx, tmp_path):
    """Make the library unopenable and return a callable that restores it."""
    if kind == "missing":
        ctx.settings["library_root"] = str(tmp_path / "gone")
        return lambda: ctx.settings.__setitem__("library_root", str(root))
    if kind == "moved":
        moved = tmp_path / "moved-away"
        root.rename(moved)
        return lambda: moved.rename(root)
    if kind == "corrupt":
        manifest = root / ".engagements" / "library.yaml"
        saved = manifest.read_bytes()
        manifest.write_text("library_id: [unterminated\n", encoding="utf-8")
        return lambda: manifest.write_bytes(saved)
    if kind == "unreadable":
        if os.name == "nt":
            pytest.skip("mode 000 does not bind on Windows")
        if os.geteuid() == 0:
            pytest.skip("root reads a mode-000 directory")
        saved = root.stat().st_mode
        root.chmod(0)
        return lambda: root.chmod(saved)
    raise AssertionError(kind)


KINDS = ["missing", "moved", "corrupt", "unreadable"]


def setup(monkeypatch, tmp_path, **kwargs):
    home = project_home(monkeypatch, tmp_path)
    root = copy_library(tmp_path)
    plugin, ctx = registered(monkeypatch, tmp_path, library=root, cwd=str(home), **kwargs)
    ctx.runtime = plugin.runtime  # the module the registered hooks really call (the plugin imports it relatively)
    return home, root, ctx


@pytest.mark.parametrize("surface", UNSUPPORTED)
@pytest.mark.parametrize("kind", KINDS)
def test_an_unsupported_turn_revokes_the_surface_while_the_library_is_unavailable(monkeypatch, tmp_path, kind, surface):
    """The reproduction: record, lose the library, unsupported turn, restore, local /client."""
    home, root, ctx = setup(monkeypatch, tmp_path, surface="cli")
    turn(ctx)  # a CLI turn at a folder no project owns: nothing bound, but the surface is recorded
    assert ctx.state.values[surface_key()] == "cli"
    restore = make_unavailable(kind, root, ctx, tmp_path)
    try:
        ctx.surface = surface
        turn(ctx)  # the library cannot open, yet the record must go
        assert ctx.state.values[surface_key()] is None
    finally:
        restore()
    ctx.surface = "cli"
    ctx.cwd = str(home / "clients" / "riverbend" / "recall")  # a folder that would auto-bind
    writes_before = len(ctx.state.writes)
    text = client(ctx)
    assert UNAVAILABLE in text and "Bound." not in text
    assert len(ctx.state.writes) == writes_before  # the command wrote nothing
    assert binding_key_for(root, "s1") not in ctx.state.values


@pytest.mark.parametrize("kind", KINDS)
def test_a_supported_turn_after_the_revocation_records_again_once_the_library_is_back(monkeypatch, tmp_path, kind):
    home, root, ctx = setup(monkeypatch, tmp_path, surface="cli")
    turn(ctx)
    restore = make_unavailable(kind, root, ctx, tmp_path)
    try:
        ctx.surface = "telegram"
        turn(ctx)
    finally:
        restore()
    ctx.surface = "desktop"
    ctx.cwd = str(home / "clients" / "riverbend" / "recall")
    turn(ctx)
    assert ctx.state.values[surface_key()] == "desktop"
    assert "Bound. Client: Riverbend Dental" in client(ctx)


@pytest.mark.parametrize("kind", KINDS)
def test_an_unavailable_library_writes_no_state_for_a_user_who_never_configured_one(monkeypatch, tmp_path, kind):
    """Clearing an absent key writes nothing, and a supported turn does not create a record with no library."""
    home, root, ctx = setup(monkeypatch, tmp_path, surface="cli")
    make_unavailable(kind, root, ctx, tmp_path)
    for surface in ("cli", "desktop", "tui", "telegram", "local", "acp", ""):
        ctx.surface = surface
        assert turn(ctx) is None
    assert ctx.state.writes == []
    assert ctx.state.values == {}


@pytest.mark.parametrize("kind", KINDS)
def test_an_existing_record_follows_a_move_between_supported_surfaces_without_the_library(monkeypatch, tmp_path, kind):
    home, root, ctx = setup(monkeypatch, tmp_path, surface="cli")
    turn(ctx)
    make_unavailable(kind, root, ctx, tmp_path)
    ctx.surface = "desktop"
    turn(ctx)
    assert ctx.state.values[surface_key()] == "desktop"


def test_the_revocation_does_not_depend_on_binding_maintenance_failing_cleanly(monkeypatch, tmp_path):
    """Not just a missing library: any failure in maintenance still leaves the clear in place."""
    home, root, ctx = setup(monkeypatch, tmp_path, surface="desktop")
    turn(ctx)

    def boom(*args, **kwargs):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(ctx.runtime, "_maintain_binding", boom)
    ctx.surface = "telegram"
    assert turn(ctx) is None  # the hook never raises into the turn
    assert ctx.state.values[surface_key()] is None


def test_a_session_id_less_turn_touches_no_surface_state(monkeypatch, tmp_path):
    home, root, ctx = setup(monkeypatch, tmp_path, surface="cli")
    turn(ctx)
    before = dict(ctx.state.values)
    ctx.surface = "telegram"
    assert ctx.hook("pre_llm_call", session_id=None, user_message="hi", conversation_history=[],
                    is_first_turn=False, model="stub", platform="telegram") is None
    assert ctx.state.values == before


def test_a_supported_turn_with_a_failing_state_store_still_returns_normally(monkeypatch, tmp_path):
    home, root, ctx = setup(monkeypatch, tmp_path, surface="cli")
    ctx.state.fail = OSError("state unavailable")
    assert turn(ctx) is None
    assert ctx.state.values == {}


# --- the sibling hooks ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
def test_compaction_never_touches_the_surface_record_and_writes_nothing_without_a_library(monkeypatch, tmp_path, kind):
    home, root, ctx = setup(monkeypatch, tmp_path, surface="desktop")
    turn(ctx)
    make_unavailable(kind, root, ctx, tmp_path)
    before, writes = dict(ctx.state.values), list(ctx.state.writes)
    ctx.hook("post_compaction", session_id="s2", old_session_id="s1")
    ctx.hook("post_compaction", session_id="s1")
    assert ctx.state.values == before and ctx.state.writes == writes


@pytest.mark.parametrize("kind", KINDS)
def test_reset_and_delete_clear_the_surface_when_the_library_is_unavailable(monkeypatch, tmp_path, kind):
    home, root, ctx = setup(monkeypatch, tmp_path, surface="desktop")
    for sid in ("s1", "s2", "s3"):
        turn(ctx, sid)
    make_unavailable(kind, root, ctx, tmp_path)
    ctx.hook("on_session_reset", session_id="s1")
    ctx.hook("on_session_delete", session_id="s2", deleted_ids=["s2", "s3"])
    assert [ctx.state.values[surface_key(sid)] for sid in ("s1", "s2", "s3")] == [None, None, None]


def test_a_failing_surface_clear_does_not_keep_reset_and_delete_from_pruning_the_rest(monkeypatch, tmp_path):
    home, root, ctx = setup(monkeypatch, tmp_path, surface="cli")
    runtime = ctx.runtime
    ctx.cwd = str(home / "clients" / "riverbend" / "recall")
    for sid in ("s1", "s2"):
        turn(ctx, sid)
        assert ctx.state.values[binding_key_for(root, sid)] is not None
    real = runtime._forget_surface

    def failing(store, session_id):
        raise OSError("surface key unreadable")

    monkeypatch.setattr(runtime, "_forget_surface", failing)
    ctx.hook("on_session_reset", session_id="s1")
    ctx.hook("on_session_delete", session_id="s2", deleted_ids=["s2"])
    monkeypatch.setattr(runtime, "_forget_surface", real)
    assert ctx.state.values[binding_key_for(root, "s1")] is None
    assert ctx.state.values[binding_key_for(root, "s2")] is None


# --- the no-Project cwd is unbound only because no registered location contains it ---------------


def edit_recall_location(root, location):
    path = root / "Riverbend Dental" / "Projects" / "Recall Campaign.md"
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("locations: [~/clients/riverbend/recall]", f"locations: [{location}]"),
                    encoding="utf-8")


def test_a_location_that_contains_desktops_default_workdir_binds(monkeypatch, tmp_path):
    """The control for the no-Project case: unbound is a property of the locations, not a home-directory ban."""
    home = project_home(monkeypatch, tmp_path)
    for number, location in enumerate((str(home), str(home.parent))):  # equal to it, and containing it
        library = tmp_path / f"library-{number}"
        shutil.copytree(FIXTURES / "studio", library)
        edit_recall_location(library, location)
        _, ctx = registered(monkeypatch, tmp_path, library=library.resolve(), cwd=str(home), surface="desktop")
        banner = turn(ctx)
        assert banner and "Recall Campaign" in banner["context"], location


def test_desktops_default_workdir_is_unbound_when_no_location_contains_it(monkeypatch, tmp_path):
    home, root, ctx = setup(monkeypatch, tmp_path, surface="desktop")
    assert turn(ctx) is None
    assert "Not bound." in client(ctx) and binding_key_for(root, "s1") not in ctx.state.values
