"""Bulk session deletion must prune every deduplicated id (spec A10).

``ids[:500]`` used to keep the binding and banner state of every session after the 500th;
Hermes supplies the complete deleted-session list.
"""

from plugin_support import registered
from tools_support import banner_key_for, binding_key_for, copy_library


def setup(monkeypatch, tmp_path):
    root = copy_library(tmp_path)
    plugin, ctx = registered(monkeypatch, tmp_path, library=root)
    return root, plugin, ctx


def test_bulk_delete_prunes_every_deduplicated_id_beyond_five_hundred(monkeypatch, tmp_path):
    root, _, ctx = setup(monkeypatch, tmp_path)
    ids = [f"session-{index:04d}" for index in range(501)]
    for session_id in ids:
        ctx.state.values[binding_key_for(root, session_id)] = {
            "mode": "explicit", "client": "acme-bakery", "at": "2026-11-07T14:02:00-06:00"}
        ctx.state.values[banner_key_for(session_id)] = {"sig": "{}", "compacted": False}
    ctx.hook("on_session_delete", session_id=ids[0], reason="prune", profile=None, deleted_ids=ids)
    assert [s for s in ids if ctx.state.values[binding_key_for(root, s)] is not None] == []
    assert [s for s in ids if ctx.state.values[banner_key_for(s)] is not None] == []


def test_bulk_delete_prunes_the_last_listed_id_after_duplicates(monkeypatch, tmp_path):
    root, _, ctx = setup(monkeypatch, tmp_path)
    ids = [f"s{index:04d}" for index in range(600)]
    for session_id in ids:
        ctx.state.values[binding_key_for(root, session_id)] = {"mode": "off"}
    listed = [ids[0], ids[0], *ids[1:]]
    ctx.hook("on_session_delete", session_id=ids[0], reason="prune", profile=None, deleted_ids=listed)
    assert ctx.state.values[binding_key_for(root, ids[-1])] is None
