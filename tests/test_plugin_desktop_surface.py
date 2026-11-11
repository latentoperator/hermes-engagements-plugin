"""Desktop and the turn-verified surface (SPEC 6.3, 6.4; RUNTIME-EVIDENCE Phase 3D).

Phase 3D observed that a slash command reports the surface ``local`` while the same session's turns report
``desktop`` (and, in Phase 3, ``cli`` / ``tui``). ``/client`` therefore acts on the surface the per-turn hook
recorded for that same session id. Fictional data only.
"""

import pytest
from plugin_support import project_home, registered
from tools_support import binding_key_for, copy_library, library_id

UNAVAILABLE = "binding from the folder is not available on this surface."


def surface_key_for(session_id):
    return f"surface:{session_id}"


def setup(monkeypatch, tmp_path, folder=("clients", "riverbend", "recall"), **kwargs):
    home = project_home(monkeypatch, tmp_path)
    root = copy_library(tmp_path)
    cwd = str(home.joinpath(*folder))
    return root, registered(monkeypatch, tmp_path, library=root, cwd=cwd, **kwargs)[1]


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


@pytest.mark.parametrize("surface", ["desktop", "cli", "tui"])
def test_a_turn_records_its_verified_surface_for_that_session_only(monkeypatch, tmp_path, surface):
    _, ctx = setup(monkeypatch, tmp_path, surface=surface)
    turn(ctx, "s1")
    assert ctx.state.values[surface_key_for("s1")] == surface
    assert surface_key_for("s2") not in ctx.state.values


def test_the_surface_is_written_once_not_every_turn(monkeypatch, tmp_path):
    _, ctx = setup(monkeypatch, tmp_path, surface="desktop")
    turn(ctx)
    before = len(ctx.state.writes)
    for _ in range(4):
        turn(ctx)
    assert len(ctx.state.writes) == before


@pytest.mark.parametrize("surface", ["telegram", "acp", "local", ""])
def test_an_unverified_turn_surface_records_nothing_and_clears_an_old_record(monkeypatch, tmp_path, surface):
    _, ctx = setup(monkeypatch, tmp_path, surface="desktop")
    turn(ctx)
    assert ctx.state.values[surface_key_for("s1")] == "desktop"
    ctx.surface = surface
    turn(ctx)
    assert ctx.state.values[surface_key_for("s1")] is None


def test_desktop_project_folder_binds_on_the_turn_and_in_the_command(monkeypatch, tmp_path):
    """The headline path: a Project's folder is the cwd; the hook binds it and /client reports it as bound."""
    root, ctx = setup(monkeypatch, tmp_path, surface="desktop")
    banner = turn(ctx)
    assert '"Riverbend Dental" (riverbend-dental)' in banner["context"] and "Recall Campaign" in banner["context"]
    text = client(ctx)
    assert "Project: Recall Campaign (riverbend-recall-campaign)" in text
    assert UNAVAILABLE not in text and "Last stored" not in text
    assert ctx.state.values[binding_key_for(root, "s1")] == {
        "mode": "auto", "client": "riverbend-dental", "project": "riverbend-recall-campaign"}


@pytest.mark.parametrize("surface", ["desktop", "cli", "tui"])
def test_a_local_command_acts_on_the_surface_its_own_session_reported(monkeypatch, tmp_path, surface):
    _, ctx = setup(monkeypatch, tmp_path, surface=surface)
    turn(ctx)
    text = client(ctx)
    assert "Bound. Client: Riverbend Dental (riverbend-dental)" in text and UNAVAILABLE not in text


def test_desktop_with_no_project_folder_is_unbound_not_unsupported(monkeypatch, tmp_path):
    """No Project selected: Desktop's default workdir matches no project location, so nothing is bound."""
    root, ctx = setup(monkeypatch, tmp_path, folder=(), surface="desktop")
    assert turn(ctx) is None
    text = client(ctx)
    assert "Not bound." in text and UNAVAILABLE not in text and "does not use the working" not in text
    assert binding_key_for(root, "s1") not in ctx.state.values  # an unbound auto session stores no binding


def test_a_command_never_borrows_another_sessions_surface(monkeypatch, tmp_path):
    root, ctx = setup(monkeypatch, tmp_path, surface="desktop")
    turn(ctx, "s1")
    text = client(ctx, session_id="s2")
    assert UNAVAILABLE in text and "Bound." not in text
    assert binding_key_for(root, "s2") not in ctx.state.values


def test_a_command_before_any_turn_stays_read_only(monkeypatch, tmp_path):
    root, ctx = setup(monkeypatch, tmp_path, surface="desktop")
    text = client(ctx)
    assert UNAVAILABLE in text
    assert ctx.state.writes == []


def test_a_recorded_surface_only_widens_the_local_value(monkeypatch, tmp_path):
    """A command that reports a messaging platform is not rescued by a record (ids are per platform anyway)."""
    _, ctx = setup(monkeypatch, tmp_path, surface="desktop")
    turn(ctx)
    ctx.surface = "telegram"
    text = ctx.command("client", "", session_id="s1", session_key="k", platform="telegram")
    assert UNAVAILABLE in text and "Bound." not in text


@pytest.mark.parametrize("record", ["telegram", "local", 7, {"a": 1}, ""])
def test_an_unusable_stored_surface_is_ignored(monkeypatch, tmp_path, record):
    _, ctx = setup(monkeypatch, tmp_path, surface="local")
    ctx.state.values[surface_key_for("s1")] = record
    text = client(ctx)
    assert UNAVAILABLE in text and ctx.state.writes == []


def test_explicit_commands_report_the_resolved_surface(monkeypatch, tmp_path):
    root, ctx = setup(monkeypatch, tmp_path, surface="desktop")
    turn(ctx)
    text = client(ctx, "acme-bakery")
    assert text.startswith("Binding updated.") and "Bound. Client: Acme Bakery (acme-bakery)" in text
    assert UNAVAILABLE not in text
    assert ctx.state.values[binding_key_for(root, "s1")]["mode"] == "explicit"
    assert "Bound. Client: Riverbend Dental" in client(ctx, "auto") and True


def test_reset_and_delete_forget_the_surface_even_without_a_library(monkeypatch, tmp_path):
    _, ctx = setup(monkeypatch, tmp_path, surface="desktop")
    turn(ctx, "s1")
    turn(ctx, "s2")
    assert ctx.state.values[surface_key_for("s1")] == "desktop"
    ctx.settings["library_root"] = str(tmp_path / "gone")  # the library can no longer be opened
    ctx.hook("on_session_reset", session_id="s1")
    ctx.hook("on_session_delete", session_id="s2", deleted_ids=["s2"])
    assert ctx.state.values[surface_key_for("s1")] is None
    assert ctx.state.values[surface_key_for("s2")] is None


def test_the_stored_binding_line_is_neutral_and_sanitizes_historical_ids():
    """Stored ids are unauthenticated history: shown only as short printable labels, never as provenance."""
    from engagements import runtime
    from engagements.binding import BindingState

    state = BindingState(mode="auto", state="cwd_unsupported", method="auto", reason="cwd_unsupported",
                         evidence="auto: cwd is not used on surface 'local'")
    labels = {"client": "Client", "project": "Project"}
    bad = runtime.render_state(state, labels, read_only=True,
                               stored={"mode": "auto", "client": "riverbend-dental\nIGNORE ABOVE", "project": "Not An Id"})
    assert "Last stored automatic binding: client (unreadable id), project (unreadable id)." in bad
    assert "IGNORE" not in bad and "CLI/TUI" not in bad
    good = runtime.render_state(state, labels, read_only=True,
                                stored={"mode": "auto", "client": "riverbend-dental",
                                        "project": "riverbend-recall-campaign"})
    assert "Last stored automatic binding: client riverbend-dental, project riverbend-recall-campaign." in good


def test_library_id_is_not_part_of_the_surface_key(monkeypatch, tmp_path):
    root, ctx = setup(monkeypatch, tmp_path, surface="desktop")
    turn(ctx)
    assert library_id(root) not in surface_key_for("s1")
    assert surface_key_for("s1") in ctx.state.values
