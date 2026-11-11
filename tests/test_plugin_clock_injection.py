"""The plugin tests must not read the wall clock.

The old ``test_plugin_commands`` assertion accepted only ``explicit 0...`` or ``explicit 1...`` while
the host clock was real, so a valid 20:00-23:59 clock failed the suite. ``registered()`` pins the
host clock to ``tools_support.fixed_clock`` before registration.
"""

from plugin_support import registered
from tools_support import copy_library


def test_explicit_binding_evidence_uses_the_injected_clock(monkeypatch, tmp_path):
    root = copy_library(tmp_path)
    _, ctx = registered(monkeypatch, tmp_path, library=root)
    assert ctx.call_tool("engagements_bind", {"client": "acme-bakery"}, session_id="s1")["ok"] is True
    text = ctx.command("client", "", session_id="s1", session_key="key", platform="local")
    assert "Evidence: explicit 14:02" in text  # tools_support.CLOCK_TIME, whatever the real clock says
