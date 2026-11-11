"""Print the README's example output by running the real plugin against the fixtures.

Usage (from the repository root):

    uv run --no-project --python 3.14 --with 'pyyaml>=6,<7' python scripts/readme_examples.py

Every block between ``===== <name>`` markers is produced by the real tool handlers,
commands and CLI over a throwaway copy of ``tests/fixtures/studio`` (fictional data
only). The throwaway library path is printed as ``<library>`` so the output is
portable. Paste the blocks into README.md unchanged; do not hand-edit them.
"""

import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

from engagements import runtime  # noqa: E402
from engagements.tools import call_tool  # noqa: E402
from plugin_support import FakeCtx, load_plugin  # noqa: E402
from tools_support import fixed_clock, make_session  # noqa: E402

BLOCKS = []


def block(title, value):
    text = value if isinstance(value, str) else json.dumps(value, indent=2, ensure_ascii=False)
    BLOCKS.append(f"===== {title}\n{text}\n")


def rev(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]


tmp = Path(tempfile.mkdtemp(prefix="eng-readme-"))
try:
    root = (tmp / "library").resolve()
    shutil.copytree(REPO / "tests" / "fixtures" / "studio", root)

    home = tmp / "home"
    for relative in ("clients/acme/website-2026",):
        (home / relative).mkdir(parents=True)
    os.environ["HOME"] = os.environ["USERPROFILE"] = str(home)  # expanduser: HOME on POSIX, USERPROFILE on Windows

    session, store = make_session(root, session_id="sess-readme", cwd=str(root))

    block("find-acme", call_tool("engagements_find", {"query": "acme"}, session))
    block("create-client", call_tool("engagements_create", {
        "kind": "client", "name": "Harbor Print Shop",
        "sections": [{"section": "Summary", "body": "One-shop print house. Owner: Marisol Vega."}]},
        session))

    created = root / "Harbor Print Shop" / "Client.md"
    block("create-project", call_tool("engagements_create", {
        "kind": "project", "name": "Harbor Signage 2027", "client": "harbor-print-shop",
        "properties": {"locations": ["~/clients/harbor/signage"]},
        "sections": [{"section": "Purpose", "body": "Replace the shopfront and van livery."}]},
        session))

    block("bind-acme", call_tool("engagements_bind", {"client": "acme-bakery"}, session))

    block("update-contact", call_tool("engagements_update", {
        "id": "harbor-print-shop", "base_rev": rev(created), "source": "call with Marisol",
        "ops": [{"op": "append_to_section", "section": "Contacts",
                 "text": "- Marisol Vega | owner | marisol@harbor-print.example | mornings"}]}, session))

    # Conflict: an external editor changes Contacts after this rev was read.
    stale = rev(created)
    text = created.read_text(encoding="utf-8")
    created.write_text(text.replace("## Contacts\n", "## Contacts\n- Desk line | shop | (555) 010-0100\n"),
                       encoding="utf-8", newline="\n")  # records are LF on every platform
    block("stale-update", call_tool("engagements_update", {
        "id": "harbor-print-shop", "base_rev": stale, "source": "call with Marisol",
        "ops": [{"op": "replace_line", "section": "Contacts", "match": "Marisol Vega",
                 "text": "- Marisol Vega | owner | marisol@harbor-print.example | afternoons"}]}, session))

    class Settings:
        def __init__(self):
            self.values = {"library_root": str(root)}

        def get(self, key, default=None):
            return self.values.get(key, default)

        def set(self, key, value):
            self.values[key] = value

    settings = Settings()
    host = runtime.Host(get_setting=settings.get, default_root=lambda: root,
                        state_get=lambda key: store.get(key),
                        state_set=lambda key, value: store.__setitem__(key, value),
                        session_cwd=lambda: str(root), session_surface=lambda: "cli",
                        profile=lambda: "default", clock=fixed_clock)

    # The per-turn banner in its current format, from the real hook over a synthetic host.
    banner_store = {}
    banner_host = runtime.Host(get_setting=settings.get, default_root=lambda: root,
                               state_get=lambda key: banner_store.get(key),
                               state_set=lambda key, value: banner_store.__setitem__(key, value),
                               session_cwd=lambda: str(home / "clients/acme/website-2026"),
                               session_surface=lambda: "cli", profile=lambda: "studio-dev",
                               clock=fixed_clock)
    banner = runtime.banner_hook(banner_host, session_id="sess-banner")
    block("banner", (banner or {}).get("context", ""))

    plugin = load_plugin()
    plugin._now = fixed_clock
    ctx = FakeCtx(settings={"library_root": str(root)},
                  cwd=str(home / "clients/acme/website-2026"), surface="cli")
    plugin.register(ctx)
    block("client-name", ctx.command("client", "acme-bakery", session_id="sess-readme"))
    block("client-list", ctx.command("client", "list", session_id="sess-readme"))
    block("doctor", ctx.command("engagements", "doctor"))
    block("cli-show",
          runtime.cli_run(host, SimpleNamespace(engagements_command="show", id="acme-bakery"))[1])
finally:
    shutil.rmtree(tmp, ignore_errors=True)

portable = "\n".join(BLOCKS).replace(str(root), "<library>").replace(str(home), "<home>")
print(portable, end="")
