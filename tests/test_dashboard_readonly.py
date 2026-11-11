"""A GET may not create anything: the dashboard's read-only contract on real filesystems.

Three writes used to happen on view paths (the library lock, a recreated library, and the
host state lock). Every test here inventories the library root and the scratch ``HERMES_HOME``
before and after and compares them byte for byte, so "it did not write" is a fact about the
disk rather than an assertion about a mock. The state-reader test uses the installed Hermes
``PluginState`` when this interpreter can import it, and skips when it cannot unless the
environment demands a host, in which case it fails.
"""

import importlib
import importlib.util
import json
import os
import re
import shutil
import sys
import types
import uuid
from pathlib import Path

import pytest
from engagements import dashboard_data, runtime
from test_dashboard import _HTTPException, _Router, load_api, make_host
from tools_support import FIXTURES, copy_library, library_id

ROOT = Path(__file__).resolve().parent.parent


def inventory(*roots):
    """Every file and directory under each root, with file bytes, keyed by path."""
    found = {}
    for root in roots:
        root = Path(root)
        if not root.exists():
            found[str(root)] = "absent"
            continue
        for path in sorted(root.rglob("*")):
            found[f"{root}::{path.relative_to(root)}"] = path.read_bytes() if path.is_file() else "dir"
    return found


def write_metadata(root, text):
    meta = Path(root) / ".engagements"
    meta.mkdir(parents=True, exist_ok=True)
    (meta / "library.yaml").write_text(text, encoding="utf-8")


def valid_metadata():
    return f"format: 1\nlibrary_id: {uuid.uuid4()}\n"


# --- the library open -----------------------------------------------------------------


def test_invalid_metadata_is_unavailable_and_creates_no_lock(tmp_path):
    """The writing opener re-reads invalid metadata under a lock it creates; a view must not."""
    root = tmp_path / "library"
    (root / ".engagements" / "locks").mkdir(parents=True)
    write_metadata(root, "format: 1\nlibrary_id: not-a-uuid\n")
    home = tmp_path / "hermes-home"
    host, _ = make_host({"library_root": str(root)})
    before = inventory(root, home)

    assert dashboard_data.overview(host)["status"] == "unavailable"
    assert dashboard_data.record(host, "acme-bakery")["status"] == "unavailable"
    assert dashboard_data.session_view(host, "s-dash")["status"] == "unavailable"
    assert dashboard_data.doctor_view(host)["status"] == "ok"  # doctor classifies without the opener

    assert inventory(root, home) == before
    assert not (root / ".engagements" / "locks" / "_library.lock").exists()


def test_a_healthy_library_is_read_without_changing_a_byte(tmp_path):
    """Control: the read-only views work on a normal library and still write nothing."""
    root = copy_library(tmp_path)
    home = tmp_path / "hermes-home"
    host, store = make_host({"library_root": str(root)})
    before = inventory(root, home)

    assert dashboard_data.overview(host)["status"] == "ok"
    assert dashboard_data.record(host, "riverbend-dental")["status"] == "ok"
    assert dashboard_data.session_view(host, "s-dash")["status"] == "ok"
    assert dashboard_data.doctor_view(host)["status"] == "ok"

    assert inventory(root, home) == before
    assert store.writes == []


def test_a_library_that_vanishes_during_a_session_read_is_not_recreated(tmp_path, monkeypatch):
    """The session helper used to reopen with create_default and mint a new id."""
    home = tmp_path / "hermes-home"
    default = home / "plugin-data" / "engagements" / "library"
    shutil.copytree(FIXTURES / "studio", default)
    library_yaml = default / ".engagements" / "library.yaml"
    original_id = library_yaml.read_text(encoding="utf-8")
    host, _ = make_host({}, default_root=lambda: default)

    real_open = dashboard_data._open
    seen = {}

    def open_then_remove(config):
        lib, status, message = real_open(config)
        library_yaml.unlink()
        seen["after_removal"] = inventory(default, home)
        return lib, status, message

    monkeypatch.setattr(dashboard_data, "_open", open_then_remove)
    data = dashboard_data.session_view(host, "s-dash")

    assert data["status"] == "ok", "the view reports the library it opened, not a second one"
    assert not library_yaml.exists(), "the metadata must not be recreated"
    assert not (default / ".engagements" / "history").exists()
    assert not (default / ".engagements" / "snapshots").exists()
    assert not (default / ".engagements" / "locks").exists()
    assert inventory(default, home) == seen["after_removal"]
    assert library_yaml.exists() is False and original_id  # the original file was real


def test_a_missing_library_stays_missing(tmp_path):
    """Control: the missing-library path creates nothing either."""
    root = tmp_path / "not-yet"
    home = tmp_path / "hermes-home"
    host, _ = make_host({"library_root": str(root)})
    before = inventory(root, home)
    assert dashboard_data.overview(host)["status"] == "missing"
    assert dashboard_data.session_view(host, "s-dash")["status"] == "missing"
    assert inventory(root, home) == before
    assert not root.exists()


# --- the routes -------------------------------------------------------------------


def test_the_session_route_creates_no_state_file_or_lock(monkeypatch, tmp_path):
    """A GET on a profile that never wrote state must leave the plugin state directory alone."""
    root = copy_library(tmp_path)
    api, PluginState, writes = load_api(monkeypatch, tmp_path, {"library_root": str(root)})
    home = tmp_path / "hermes-home"
    before = inventory(home)

    data = api.session(id="20260811_120000_abcdef12")
    assert data["status"] == "ok" and data["client"] is None

    assert inventory(home) == before
    assert not home.exists(), "no plugin-data directory, no state file, no lock"
    assert writes == []
    assert PluginState("engagements").path.name == "state.json"


def test_the_session_route_reads_an_existing_state_file_without_the_host_lock(monkeypatch, tmp_path):
    """A state file that exists is read whole; the host's read-lock file is never created."""
    root = copy_library(tmp_path)
    home = tmp_path / "hermes-home"
    state = home / "plugin-data" / "engagements" / "state.json"
    state.parent.mkdir(parents=True)
    state.write_text("{}", encoding="utf-8")
    api, _, _ = load_api(monkeypatch, tmp_path, {"library_root": str(root)}, state={})
    before = inventory(home)

    for session_id in ("20260811_120000_abcdef12", "local-" + "a" * 64, str(uuid.uuid4()), "s-web"):
        assert api.session(id=session_id)["status"] == "ok"

    assert inventory(home) == before
    assert not (state.parent / ".state.json.lock").exists()


def test_a_malformed_session_id_is_refused_before_it_becomes_a_state_key(monkeypatch, tmp_path):
    """An unusable id must not be hashed into a key, and the error must not echo it."""
    root = copy_library(tmp_path)
    api, _, _ = load_api(monkeypatch, tmp_path, {"library_root": str(root)})
    assert api.overview()["status"] == "ok"  # loads the core, whose runtime does the hashing
    core_runtime = sys.modules["hermes_dashboard_engagements_core.runtime"]
    keys = []
    monkeypatch.setattr(core_runtime, "state_key", lambda key: keys.append(key) or key)
    home = tmp_path / "hermes-home"
    before = inventory(home)

    for bad in ("../malformed/session", "line\nbreak", "x" * 1000, "with space", "del\x00ete"):
        with pytest.raises(_HTTPException) as refused:
            api.session(id=bad)
        assert refused.value.status_code == 400
        assert refused.value.detail == "Invalid session id."
        assert len(refused.value.detail) < 40

    assert keys == [], "no malformed id is digested into a state key"
    assert inventory(home) == before


@pytest.mark.parametrize("record_id", ["..", "../etc/passwd", "Upper-Case", "a" * 1000, "line\nbreak", ""])
def test_a_malformed_record_id_is_refused_without_reflecting_it(record_id, monkeypatch, tmp_path):
    root = copy_library(tmp_path)
    api, _, _ = load_api(monkeypatch, tmp_path, {"library_root": str(root)})
    with pytest.raises(_HTTPException) as refused:
        api.record(record_id)
    assert refused.value.status_code == 400
    assert refused.value.detail == "Invalid record id."
    assert not record_id or record_id not in refused.value.detail


def test_a_legitimate_record_id_still_resolves(monkeypatch, tmp_path):
    root = copy_library(tmp_path)
    api, _, _ = load_api(monkeypatch, tmp_path, {"library_root": str(root)})
    assert api.record("riverbend-dental")["name"] == "Riverbend Dental"


# --- the installed host state reader -------------------------------------------------------

# When the environment demands an importable host, its absence fails the test instead of
# skipping it, so a run that is supposed to exercise the real host cannot pass without it.
HOST_ENV = "ENGAGEMENTS_REQUIRE_HERMES_HOST"
HOST_MODULES = ("hermes_constants", "hermes_cli.config", "hermes_cli.plugins_state")


def _host_module_absent(exc):
    """True only when the absent module is the host itself, not a dependency of it."""
    return isinstance(exc, ModuleNotFoundError) and (exc.name == "hermes_cli" or exc.name in HOST_MODULES)


def _host_unavailable(exc):
    message = f"the Hermes host module {exc.name!r} is not installed"
    if os.environ.get(HOST_ENV, "") == "1":
        pytest.fail(f"{message}; {HOST_ENV} is set, so this test must not skip")
    pytest.skip(message)


def _real_host():
    """The installed Hermes modules, or a skip (a failure when the host is demanded).

    Only the host modules themselves being missing counts as "no host in this interpreter";
    a missing dependency inside one of them, or any other import error, is raised so it
    cannot hide behind a skip.
    """
    modules = []
    for name in HOST_MODULES:
        try:
            modules.append(importlib.import_module(name))
        except ModuleNotFoundError as exc:
            if not _host_module_absent(exc):
                raise
            _host_unavailable(exc)
    return tuple(modules)


def _real_api(monkeypatch, home):
    """``plugin_api`` loaded against the installed Hermes modules; only FastAPI is stood in."""
    fastapi = types.ModuleType("fastapi")
    fastapi.APIRouter = _Router
    fastapi.HTTPException = _HTTPException
    monkeypatch.setitem(sys.modules, "fastapi", fastapi)
    monkeypatch.setenv("HERMES_HOME", str(home))
    for name in [m for m in sys.modules if m.startswith("hermes_dashboard_engagements_core")]:
        monkeypatch.delitem(sys.modules, name)
    spec = importlib.util.spec_from_file_location("engagements_plugin_api_host_test", ROOT / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _real_home(monkeypatch, tmp_path):
    """A scratch HERMES_HOME, set before the host reader is constructed: never the real one."""
    home = tmp_path / "hermes-home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def test_the_host_state_lock_is_only_taken_by_a_real_get(monkeypatch, tmp_path):
    """Control for the two tests below: the host's own reader does create the lock file."""
    _, _, plugins_state = _real_host()
    home = _real_home(monkeypatch, tmp_path)
    state = plugins_state.PluginState("engagements")
    assert home in state.data_dir.parents, "HERMES_HOME must reach the reader"

    assert state.get("binding:library:session") is None
    assert (state.data_dir / ".state.json.lock").exists()


def test_the_route_creates_no_state_lock_with_the_real_reader(monkeypatch, tmp_path):
    _, _, plugins_state = _real_host()
    home = _real_home(monkeypatch, tmp_path)
    library = home / "plugin-data" / "engagements" / "library"
    library.mkdir(parents=True)
    write_metadata(library, valid_metadata())
    state = plugins_state.PluginState("engagements")
    api = _real_api(monkeypatch, home)
    assert api.overview()["status"] == "ok"  # an initialized home, as in a running Hermes
    before = inventory(home)

    data = api.session(id="20260811_120000_abcdef12")
    assert data["status"] == "ok"
    assert inventory(home) == before
    assert not (state.data_dir / ".state.json.lock").exists()


def test_the_route_reads_an_existing_real_state_file_without_the_lock(monkeypatch, tmp_path):
    _, _, plugins_state = _real_host()
    home = _real_home(monkeypatch, tmp_path)
    library = home / "plugin-data" / "engagements" / "library"
    shutil.copytree(FIXTURES / "studio", library)
    state = plugins_state.PluginState("engagements")
    assert home in state.data_dir.parents, "HERMES_HOME must reach the reader"
    state.data_dir.mkdir(parents=True)
    key = runtime.state_key(f"binding:{library_id(library)}:20260811_120000_abcdef12")
    state.path.write_text(json.dumps({key: {"mode": "explicit", "client": "acme-bakery"}}), encoding="utf-8")
    api = _real_api(monkeypatch, home)
    assert api.overview()["status"] == "ok"  # an initialized home, as in a running Hermes
    before = inventory(home)

    data = api.session(id="20260811_120000_abcdef12")
    assert data["status"] == "ok"
    assert data["client"]["id"] == "acme-bakery", "the binding was read from the host's state file"
    assert inventory(home) == before
    assert not (state.data_dir / ".state.json.lock").exists()


def _host_skeleton(monkeypatch, tmp_path, config):
    """What the host's own home setup creates in an empty home, measured rather than listed."""
    twin = tmp_path / "twin-home"
    monkeypatch.setenv("HERMES_HOME", str(twin))
    config.ensure_hermes_home()
    return {str(path.relative_to(twin)) for path in twin.rglob("*")}


@pytest.mark.parametrize("route", ["overview", "session"])
def test_a_cold_home_gets_only_the_hosts_own_skeleton(route, monkeypatch, tmp_path):
    """SPEC 6.7's first host-owned exception, measured from the first request.

    On a Hermes home nothing has initialized yet, ``load_config_readonly`` runs the host's
    ``ensure_hermes_home`` (its standard directories and ``SOUL.md``) on its first uncached read.
    That is the host's setup; this test pins that the dashboard adds nothing to it: no library
    file, no plugin data, no state or lock.
    """
    _, config, plugins_state = _real_host()
    skeleton = _host_skeleton(monkeypatch, tmp_path, config)
    assert "SOUL.md" in skeleton and "sessions" in skeleton, "control: the host does create its skeleton"
    home = _real_home(monkeypatch, tmp_path)
    library = home / "plugin-data" / "engagements" / "library"
    library.mkdir(parents=True)
    write_metadata(library, valid_metadata())
    before = {str(path.relative_to(home)) for path in home.rglob("*")}
    library_before = inventory(library)
    api = _real_api(monkeypatch, home)

    data = api.overview() if route == "overview" else api.session(id="20260811_120000_abcdef12")

    assert data["status"] == "ok"
    created = {str(path.relative_to(home)) for path in home.rglob("*")} - before
    assert created <= skeleton, f"created beyond the host skeleton: {sorted(created - skeleton)}"
    assert inventory(library) == library_before
    assert not plugins_state.PluginState("engagements").path.exists()
    assert not (plugins_state.PluginState("engagements").data_dir / ".state.json.lock").exists()


def _names(home):
    """Paths under *home*, with the host's backup timestamps folded so two homes compare."""
    return {re.sub(r"\.(\w+)\.\d{8}-\d{6}$", r".\1.<time>", str(path.relative_to(home))) for path in home.rglob("*")}


def _initialized_home(home, config):
    """A Hermes home a running Hermes has already set up, with a settings file of its own."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("HERMES_HOME", str(home))
        config.ensure_hermes_home()
    (home / "config.yaml").write_text("plugins: {}\n", encoding="utf-8")
    library = home / "plugin-data" / "engagements" / "library"
    library.mkdir(parents=True)
    write_metadata(library, valid_metadata())
    return library


@pytest.mark.parametrize("route", ["overview", "session"])
def test_an_existing_config_gets_only_the_hosts_own_backup(route, monkeypatch, tmp_path):
    """SPEC 6.7's second host-owned exception, measured from the first request.

    When ``config.yaml`` exists and parses, the host's first uncached ``load_config_readonly``
    saves a last-known-good copy under ``backups/config/``. A twin home read by the host alone
    measures that; the dashboard's first request may create nothing beyond it, and must leave
    the settings file, the library, and plugin state untouched.
    """
    _, config, plugins_state = _real_host()
    twin = tmp_path / "twin-home"
    _initialized_home(twin, config)
    twin_before = _names(twin)
    monkeypatch.setenv("HERMES_HOME", str(twin))
    config.load_config_readonly()
    host_made = _names(twin) - twin_before
    assert any(name.startswith("backups/config/config.yaml.good.") for name in host_made), (
        "control: the host itself keeps a last-known-good copy"
    )

    home = _real_home(monkeypatch, tmp_path)
    library = _initialized_home(home, config)
    before = _names(home)
    settings_before = (home / "config.yaml").read_bytes()
    library_before = inventory(library)
    api = _real_api(monkeypatch, home)

    data = api.overview() if route == "overview" else api.session(id="20260811_120000_abcdef12")

    assert data["status"] == "ok"
    created = _names(home) - before
    assert created <= host_made, f"created beyond the host's own maintenance: {sorted(created - host_made)}"
    assert (home / "config.yaml").read_bytes() == settings_before
    assert inventory(library) == library_before
    assert not plugins_state.PluginState("engagements").path.exists()
    assert not (plugins_state.PluginState("engagements").data_dir / ".state.json.lock").exists()


# --- the host switch itself -------------------------------------------------------------------

# ``pytest.skip`` and ``pytest.fail`` raise ``BaseException`` subclasses, so ``pytest.raises``
# does not catch a skip: it leaves the test body, and pytest reports the test as skipped, which
# still exits 0. These three tests exist to pin which outcome the switch produces, so each one
# records the exception its call raised and asserts on it here, where a skip is an ordinary
# value and cannot quietly stand in for the failure it was supposed to prove.


def _raised(call):
    """The exception *call*, a zero-argument callable, ended with; ``None`` if it returned."""
    try:
        call()
    except pytest.skip.Exception as skipped:
        return skipped
    except (Exception, pytest.fail.Exception) as raised:
        return raised
    return None


def _assert_raised(raised, expected, match):
    assert isinstance(raised, expected), f"expected {expected.__name__}, got {raised!r}"
    assert re.search(match, str(raised)), raised


def test_a_demanded_host_fails_the_run_instead_of_skipping(monkeypatch):
    """A run set up to exercise the real host must fail, not pass with these tests absent."""
    monkeypatch.setattr(sys.modules[__name__], "HOST_MODULES", ("hermes_host_module_that_is_absent",))
    monkeypatch.setenv(HOST_ENV, "1")
    _assert_raised(_raised(_real_host), pytest.fail.Exception, "must not skip")


def test_an_absent_host_still_skips_when_nothing_demands_it(monkeypatch):
    """On an interpreter without a host the ordinary run skips: the host is optional here."""
    monkeypatch.setattr(sys.modules[__name__], "HOST_MODULES", ("hermes_host_module_that_is_absent",))
    monkeypatch.delenv(HOST_ENV, raising=False)
    _assert_raised(_raised(_real_host), pytest.skip.Exception, "hermes_host_module_that_is_absent")


def test_a_missing_dependency_inside_the_host_is_not_taken_for_an_absent_host(tmp_path, monkeypatch):
    """An unrelated import error inside a host module must surface, not hide behind a skip."""
    broken = tmp_path / "hermes_constants.py"
    broken.write_text("import a_dependency_that_is_not_installed\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "hermes_constants", raising=False)
    monkeypatch.delenv(HOST_ENV, raising=False)
    _assert_raised(_raised(_real_host), ModuleNotFoundError, "a_dependency_that_is_not_installed")
