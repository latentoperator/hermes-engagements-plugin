"""Binding upkeep is not gated behind banner presentation (spec 6.4).

A ``banner: false`` turn used to return before the automatic binding was retained, so a
reparented project resolved to another client on the next read. Every case here runs with
the banner on and off and demands the same binding outcome.
"""

import json

import pytest
from plugin_support import project_home, registered
from tools_support import binding_key_for, copy_library

RECALL_PROJECT = "Riverbend Dental/Projects/Recall Campaign.md"
RIVERBEND_CLIENT = "Riverbend Dental/Client.md"
RECALL_BINDING = {"mode": "auto", "client": "riverbend-dental", "project": "riverbend-recall-campaign"}


def setup(monkeypatch, tmp_path, **kwargs):
    root = copy_library(tmp_path)
    plugin, ctx = registered(monkeypatch, tmp_path, library=root, **kwargs)
    return root, plugin, ctx


def turn(ctx, session_id="s1"):
    """One user turn: the banner the model would receive, or None."""
    result = ctx.hook("pre_llm_call", session_id=session_id, user_message="hi", conversation_history=[],
                      is_first_turn=False, model="stub", platform=ctx.surface)
    if result is None:
        return None
    assert set(result) == {"context"}
    return result["context"]


def auto_bound(monkeypatch, tmp_path, **settings):
    """A session launched in the fixture's riverbend recall folder (auto binding candidate)."""
    home = project_home(monkeypatch, tmp_path)
    return setup(monkeypatch, tmp_path, cwd=str(home / "clients" / "riverbend" / "recall"), settings=settings)


def stored_binding(root, ctx, session_id="s1"):
    return ctx.state.values.get(binding_key_for(root, session_id))


def reparent(root):
    """Move the bound project to another client: the SPEC 6.4 ``client_changed`` case."""
    record = root / RECALL_PROJECT
    text = record.read_text(encoding="utf-8")
    assert "client: riverbend-dental" in text
    record.write_text(text.replace("client: riverbend-dental", "client: acme-bakery", 1), encoding="utf-8")


def delete_project(root):
    (root / RECALL_PROJECT).unlink()


def deactivate_client(root):
    """Take the bound client out of service: the SPEC 6.4 ``record_inactive`` case."""
    record = root / RIVERBEND_CLIENT
    text = record.read_text(encoding="utf-8")
    assert "status: active" in text
    record.write_text(text.replace("status: active", "status: former", 1), encoding="utf-8")


MUTATIONS = (
    pytest.param(reparent, "client_changed", id="reparented-project"),
    pytest.param(delete_project, "record_missing", id="deleted-project"),
    pytest.param(deactivate_client, "record_inactive", id="deactivated-client"),
)


@pytest.mark.parametrize("mutate, reason", [(m.values[0], m.values[1]) for m in MUTATIONS],
                         ids=[m.id for m in MUTATIONS])
@pytest.mark.parametrize("banner", [True, False], ids=["banner-on", "banner-off"])
def test_a_drifted_automatic_binding_is_revalidated_whatever_the_banner_setting(
        monkeypatch, tmp_path, banner, mutate, reason):
    root, _, ctx = auto_bound(monkeypatch, tmp_path, banner=banner)
    first = turn(ctx)
    if banner:
        assert first is not None and "riverbend-dental" in first
    else:
        assert first is None  # presentation is off ...
    # ... but the automatic identity is still retained, so the next turn can revalidate it.
    assert stored_binding(root, ctx) == RECALL_BINDING

    mutate(root)
    second = turn(ctx)
    if banner:
        assert second is not None and "Binding is invalid" in second
    else:
        assert second is None  # nothing to show with the banner off
    assert stored_binding(root, ctx) == RECALL_BINDING  # invalid keeps the stored value

    context = ctx.call_tool("engagements_context", {}, session_id="s1")
    assert context["ok"] is False
    assert context["error"]["details"]["state"] == "invalid_binding"
    assert context["error"]["details"]["reason"] == reason
    assert "acme-bakery" not in json.dumps(context)  # never substitute another client


def test_the_banner_off_status_command_reports_the_invalid_binding(monkeypatch, tmp_path):
    root, _, ctx = auto_bound(monkeypatch, tmp_path, banner=False)
    assert turn(ctx) is None
    reparent(root)
    assert turn(ctx) is None
    text = ctx.command("client", "", session_id="s1", session_key="key", platform="local")
    assert "no longer valid" in text and "Evidence: invalid: client_changed" in text
    assert "Acme Bakery" not in text


def test_the_banner_off_tools_do_not_silently_follow_the_reparented_project(monkeypatch, tmp_path):
    root, _, ctx = auto_bound(monkeypatch, tmp_path, banner=False)
    assert turn(ctx) is None
    reparent(root)
    assert turn(ctx) is None
    context = ctx.call_tool("engagements_context", {"kind": "project"}, session_id="s1")
    assert context["ok"] is False and context["error"]["code"] == "unbound"
    assert context["error"]["details"]["state"] == "invalid_binding"
    assert context["error"]["details"]["reason"] == "client_changed"


@pytest.mark.parametrize("labels", [None, {"client": "Account", "project": "Campaign"}], ids=["default", "renamed"])
def test_an_output_only_setting_does_not_change_the_binding(monkeypatch, tmp_path, labels):
    # Audit: labels rename text only; they must neither gate nor alter binding maintenance.
    settings = {"banner": False} if labels is None else {"banner": False, "labels": labels}
    root, _, ctx = auto_bound(monkeypatch, tmp_path, **settings)
    assert turn(ctx) is None
    assert stored_binding(root, ctx) == RECALL_BINDING
    text = ctx.command("client", "", session_id="s1", session_key="key", platform="local")
    assert ("Account: Riverbend Dental" if labels else "Client: Riverbend Dental") in text
