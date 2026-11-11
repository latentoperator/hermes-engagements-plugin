"""The fictional demo kit under ``demo/`` must keep working and stay fictional.

The kit is what a demo video and a first run use, so it is held to the
plugin's own checks: after ``demo/setup.py`` copies it, the library is doctor-clean
with no notices, every active project folder binds automatically to exactly its
project, the Harbor folder stays unbound until the live onboarding adds it, and
every contact detail uses reserved fictional domains.
"""

import importlib.util
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from engagements import doctor, records
from plugin_support import registered

ROOT = Path(__file__).resolve().parent.parent
KIT = ROOT / "demo"
SETUP = KIT / "setup.py"

FOLDERS = {
    "clients/acme/website-2026": "acme-website-2026",
    "clients/acme/website-2026/notes": "acme-website-2026",
    "clients/riverbend/recall-campaign": "riverbend-recall-campaign",
    "clients/riverbend/brand-assets": "riverbend-brand-assets",
}


def run_setup(home, *args, cwd=None, **extra_env):
    env = {"HOME": str(home), "USERPROFILE": str(home), "PYTHONDONTWRITEBYTECODE": "1",
           "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""), **extra_env}
    return subprocess.run([sys.executable, str(SETUP), *args], capture_output=True, text=True, timeout=60,
                          env=env, cwd=cwd)


@pytest.fixture
def demo(tmp_path, monkeypatch):
    home = (tmp_path / "home").resolve()
    home.mkdir()
    result = run_setup(home)
    assert result.returncode == 0, result.stderr
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home, home / "engagements-demo"


def client_status(monkeypatch, tmp_path, dest, folder):
    """``/client`` for a CLI session launched in ``dest/folder``: the real binding path."""
    _, ctx = registered(monkeypatch, tmp_path, library=dest / "library", cwd=str(dest / folder))
    return ctx.command("client", "", session_id="s1", session_key="key", platform="local")


def test_setup_output_is_doctor_clean_without_notices(demo):
    home, dest = demo
    report = doctor.run_doctor(dest / "library", home=str(home))
    assert report.checked == 7
    assert report.findings == [], report.render_text()


@pytest.mark.parametrize("folder, project", sorted(FOLDERS.items()))
def test_each_project_folder_binds_to_exactly_its_project(demo, tmp_path, monkeypatch, folder, project):
    _, dest = demo
    text = client_status(monkeypatch, tmp_path, dest, folder)
    assert "Mode: auto" in text and f"({project})" in text and f"matched project {project}" in text, text


@pytest.mark.parametrize("folder", ["clients/harbor/signage-2027", "clients/maple/spring-window-2025", "paste"])
def test_harbor_and_historical_folders_stay_unbound(demo, tmp_path, monkeypatch, folder):
    _, dest = demo
    text = client_status(monkeypatch, tmp_path, dest, folder)
    assert "Not bound." in text, text


def test_client_command_on_the_demo_copy(demo, tmp_path, monkeypatch):
    home, dest = demo
    _, ctx = registered(monkeypatch, tmp_path, library=dest / "library",
                        cwd=str(dest / "clients/acme/website-2026"))
    listing = ctx.command("client", "list")
    assert "- Acme Bakery (acme-bakery)" in listing and "Riverbend Dental (riverbend-dental)" in listing
    assert "Maple Street" not in listing  # paused client, historical project
    assert "Doctor: clean" in ctx.command("engagements", "doctor")


def test_setup_refuses_a_folder_it_did_not_create_and_resets_its_own(tmp_path):
    home = (tmp_path / "home").resolve()
    (home / "engagements-demo").mkdir(parents=True)
    (home / "engagements-demo" / "keep.txt").write_text("mine", encoding="utf-8")
    refused = run_setup(home)
    assert refused.returncode != 0 and "not created by this script" in refused.stderr
    assert (home / "engagements-demo" / "keep.txt").read_text(encoding="utf-8") == "mine"

    other = home / "elsewhere"
    assert run_setup(home, "--dest", str(other)).returncode == 0
    again = run_setup(home, "--dest", str(other))
    assert again.returncode != 0 and "--reset" in again.stderr
    first_id = (other / "library/.engagements/library.yaml").read_text(encoding="utf-8")
    assert run_setup(home, "--dest", str(other), "--reset").returncode == 0
    assert (other / "library/.engagements/library.yaml").read_text(encoding="utf-8") != first_id

    assert run_setup(home, "--remove").returncode != 0  # the default folder is not ours
    assert (home / "engagements-demo" / "keep.txt").is_file()
    assert run_setup(home, "--dest", str(other), "--remove").returncode == 0
    assert not other.exists()
    assert run_setup(home, "--dest", str(other), "--remove").returncode == 0  # already gone


def test_a_custom_destination_rewrites_every_location(tmp_path):
    home = (tmp_path / "home").resolve()
    home.mkdir()
    dest = home / "video" / "kit"
    assert run_setup(home, "--dest", str(dest)).returncode == 0
    for path in list((dest / "library").rglob("*.md")) + list((dest / "paste").rglob("*.md")):
        assert "engagements-demo" not in path.read_text(encoding="utf-8"), path
    report = doctor.run_doctor(dest / "library", home=str(home))
    assert report.findings == [], report.render_text()


def refused(result, *words):
    """A clean refusal: exit 1, a message naming the reason, and no traceback."""
    assert result.returncode == 1, (result.returncode, result.stdout, result.stderr)
    assert "Traceback" not in result.stderr and "Refusing" in result.stderr, result.stderr
    for word in words:
        assert word in result.stderr, result.stderr
    return True


def make_link(link, target):
    """A directory symlink, or a junction on Windows when symlinks need privileges."""
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError:
        if sys.platform != "win32":
            pytest.skip("cannot create a directory symlink here")
        import _winapi
        _winapi.CreateJunction(str(target), str(link))


def test_setup_refuses_protected_destinations_before_writing(tmp_path):
    home = (tmp_path / "home").resolve()
    home.mkdir()
    hermes_home = (tmp_path / "hermes-elsewhere").resolve()
    cases = {
        "home": str(home),
        "tilde": "~",
        "home ancestor": str(home.parent),
        "filesystem root": home.anchor,
        "default Hermes home": str(home / ".hermes"),
        "inside default Hermes home": str(home / ".hermes" / "profiles" / "demo"),
        "inside HERMES_HOME": str(hermes_home / "demo"),
        "repository": str(ROOT),
        "inside repository": str(ROOT / "demo" / "copy"),
        "repository ancestor": str(ROOT.parent),
    }
    for name, dest in cases.items():
        for args in ((), ("--reset",), ("--remove",)):
            result = run_setup(home, "--dest", dest, *args, HERMES_HOME=str(hermes_home))
            assert refused(result), name
    assert list(home.iterdir()) == []
    assert not hermes_home.exists() and not (ROOT / "demo" / "copy").exists()
    assert not (ROOT / ".engagements-demo").exists() and not (ROOT.parent / ".engagements-demo").exists()


def test_cleanup_refuses_home_and_its_ancestors_even_with_a_genuine_marker(tmp_path):
    first = (tmp_path / "first").resolve()
    first.mkdir()
    later = (tmp_path / "later").resolve()
    assert run_setup(first, "--dest", str(later)).returncode == 0  # a genuine marker for `later`
    sentinel = later / ".hermes" / "keep.txt"
    sentinel.parent.mkdir()
    sentinel.write_text("real", encoding="utf-8")

    for args in (("--reset",), ("--remove",)):
        assert refused(run_setup(later, "--dest", str(later), *args), "home folder")  # `later` is now HOME
        inner = later / "inner-home"
        inner.mkdir(exist_ok=True)
        assert refused(run_setup(inner, "--dest", str(later), *args), "home folder")  # now an ancestor of HOME
        assert sentinel.read_text(encoding="utf-8") == "real"
    assert (later / "library" / "Acme Bakery" / "Client.md").is_file()


def test_cleanup_refuses_invalid_markers_and_deletes_a_valid_copy_in_full(tmp_path):
    home = (tmp_path / "home").resolve()
    home.mkdir()
    dest, other = home / "kit", home / "other"
    assert run_setup(home, "--dest", str(dest)).returncode == 0
    assert run_setup(home, "--dest", str(other)).returncode == 0
    marker = dest / ".engagements-demo"
    genuine = marker.read_text(encoding="utf-8")
    keep = dest / "keep.txt"
    keep.write_text("added after setup", encoding="utf-8")

    def assert_refused(*words):
        for args in (("--reset",), ("--remove",)):
            assert refused(run_setup(home, "--dest", str(dest), *args), *words)
        assert keep.is_file()

    marker.write_text("anything at all\n", encoding="utf-8")
    assert_refused("not written for this folder")
    marker.write_bytes((other / ".engagements-demo").read_bytes())  # a marker copied from another demo folder
    assert_refused("not written for this folder")
    marker.unlink()
    marker.mkdir()
    assert_refused("not a regular file")
    marker.rmdir()
    elsewhere = tmp_path / "genuine.txt"
    elsewhere.write_text(genuine, encoding="utf-8", newline="\n")
    try:
        os.symlink(elsewhere, marker)
    except OSError:
        pass  # Windows without symlink privileges: the other invalid markers still ran
    else:
        assert_refused("not a regular file")
        marker.unlink()

    marker.write_text(genuine, encoding="utf-8", newline="\n")
    assert run_setup(home, "--dest", str(dest), "--remove").returncode == 0
    assert not dest.exists()  # the whole copy, including the file added after setup
    assert elsewhere.read_text(encoding="utf-8") == genuine and other.is_dir()


def test_setup_refuses_a_nonempty_foreign_folder_without_touching_it(tmp_path):
    home = (tmp_path / "home").resolve()
    foreign = home / "projects"
    foreign.mkdir(parents=True)
    (foreign / "notes.md").write_text("mine", encoding="utf-8")
    for args in ((), ("--reset",), ("--remove",)):
        assert refused(run_setup(home, "--dest", str(foreign), *args), "not created by this script")
    assert [p.name for p in foreign.iterdir()] == ["notes.md"]


def test_link_destinations_are_refused_cleanly(tmp_path):
    home = (tmp_path / "home").resolve()
    target = home / "target"
    target.mkdir(parents=True)
    link = home / "link"
    make_link(link, target)
    for args in ((), ("--reset",), ("--remove",)):
        assert refused(run_setup(home, "--dest", str(link), *args), "link")
    assert list(target.iterdir()) == []

    real = home / "real"
    assert run_setup(home, "--dest", str(real)).returncode == 0
    alias = home / "alias"
    make_link(alias, real)
    for args in (("--reset",), ("--remove",)):
        assert refused(run_setup(home, "--dest", str(alias), *args), "link")
    assert (real / ".engagements-demo").is_file() and (real / "library").is_dir()
    assert run_setup(home, "--dest", str(real), "--remove").returncode == 0
    assert not real.exists() and target.is_dir()


def test_a_relative_destination_is_made_absolute_and_binds(tmp_path, monkeypatch):
    home = (tmp_path / "home").resolve()
    work = home / "work"
    work.mkdir(parents=True)
    assert run_setup(home, "--dest", "rel kit", cwd=work).returncode == 0
    dest = work / "rel kit"
    project = records.parse((dest / "library/Acme Bakery/Projects/Website 2026.md").read_bytes())
    assert project.properties["locations"][0] == "~/work/rel kit/clients/acme/website-2026"
    assert doctor.run_doctor(dest / "library", home=str(home)).findings == []
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    text = client_status(monkeypatch, tmp_path, dest, "clients/acme/website-2026")
    assert "matched project acme-website-2026" in text, text
    assert run_setup(home, "--dest", "rel kit", "--remove", cwd=work).returncode == 0
    assert not dest.exists()


AWKWARD = ["with space", "hash#plain", "hash # comment", "bracket[kit", "brace{kit}", "comma,kit",
           "quote'kit", "amp&kit", "percent%kit", "café kit"]
if sys.platform != "win32":  # names Windows does not allow
    AWKWARD += ["colon:kit", "colon: kit", 'dquote"kit', "back\\slash"]


@pytest.mark.parametrize("name", AWKWARD)
@pytest.mark.parametrize("under_home", [True, False], ids=["under-home", "absolute"])
def test_awkward_destination_names_stay_valid_yaml_and_bind(tmp_path, monkeypatch, name, under_home):
    home = (tmp_path / "home").resolve()
    home.mkdir()
    dest = (home / "kits" / name) if under_home else (tmp_path / "outside" / name)
    result = run_setup(home, "--dest", str(dest))
    assert result.returncode == 0, result.stderr
    shown = f"~/kits/{name}" if under_home else str(dest.resolve())

    report = doctor.run_doctor(dest / "library", home=str(home))
    assert report.checked == 7
    assert report.findings == [], report.render_text()
    for path, suffix in [("Acme Bakery/Projects/Website 2026.md", "acme/website-2026"),
                         ("Riverbend Dental/Projects/Brand Assets.md", "riverbend/brand-assets"),
                         ("Riverbend Dental/Projects/Recall Campaign.md", "riverbend/recall-campaign"),
                         ("Maple Street Florist/Projects/Spring Window 2025.md", "maple/spring-window-2025")]:
        record = records.parse((dest / "library" / path).read_bytes())
        assert record.properties["locations"][0] == f"{shown}/clients/{suffix}", path
    acme = records.parse((dest / "library/Acme Bakery/Projects/Website 2026.md").read_bytes())
    assert acme.properties["locations"][1] == "https://git.example/studio/acme-site"

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    for folder, project in [("clients/acme/website-2026", "acme-website-2026"),
                            ("clients/riverbend/brand-assets", "riverbend-brand-assets")]:
        text = client_status(monkeypatch, tmp_path, dest, folder)
        assert f"matched project {project}" in text, text
    assert run_setup(home, "--dest", str(dest), "--remove").returncode == 0


@pytest.mark.parametrize("name", ["plain-kit", "with space", "quote'kit", "amp&kit", "hash # comment"])
def test_the_printed_init_command_passes_the_library_as_one_argument(tmp_path, name):
    home = (tmp_path / "home").resolve()
    home.mkdir()
    dest = home / "kits" / name
    result = run_setup(home, "--dest", str(dest))
    assert result.returncode == 0, result.stderr
    command = result.stdout.strip().splitlines()[-1]
    library = dest.resolve() / "library"
    if sys.platform == "win32":
        if not POWERSHELL:
            pytest.skip("PowerShell is not installed")
        assert powershell_args(command) == ["-p", "engagements-demo", "engagements", "init",
                                            "--root", str(library)], command
        return
    argv = shlex.split(command)
    assert argv[:5] == ["hermes", "-p", "engagements-demo", "engagements", "init"], argv
    expected = "~/kits/plain-kit/library" if name == "plain-kit" else str(library)
    assert argv[5:] == ["--root", expected], argv


POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")


def powershell_args(command):
    """The arguments PowerShell passes to ``hermes`` when it runs ``command``."""
    assert POWERSHELL
    capture = ("[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
               "function hermes { $args | ForEach-Object { [Console]::Out.Write($_ + [char]0) } }; ")
    result = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", capture + command],
                            capture_output=True, timeout=120, check=True)
    return result.stdout.decode("utf-8").split("\0")[:-1]


def load_setup():
    spec = importlib.util.spec_from_file_location("demo_setup", SETUP)
    setup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(setup)
    return setup


@pytest.mark.skipif(not POWERSHELL, reason="PowerShell is not installed")
def test_the_windows_init_command_is_literal_in_powershell():
    setup = load_setup()
    for path in [r"C:\demo kit\library", r"C:\demo$(1+2)\library", r"C:\demo$HOME\library",
                 r"C:\back`tick\library", r"C:\it's\library", "C:\\curly\u2019s \u2018kit\u201b\\library",
                 r"D:\a,b [c] # d; e & f\library", "C:\\Users\\Zo\u00eb\\library", r"C:\plain\library"]:
        command = f"hermes -p engagements-demo engagements init --root {setup.shell_arg(path, path, windows=True)}"
        assert powershell_args(command)[-2:] == ["--root", path], command


def test_windows_paths_serialize_as_exact_yaml_strings():
    setup = load_setup()
    template = (KIT / "library/Acme Bakery/Projects/Website 2026.md").read_text(encoding="utf-8")
    for shown in [r"C:\demo kit", r"D:\a,b [c] # d: e\kit", r"\\server\share\demo's kit", "C:\\Users\\Zoë\\demo"]:
        text = setup.rewrite_text(template, shown)
        properties = records.parse(text.encode("utf-8")).properties
        assert properties["locations"] == [shown + "/clients/acme/website-2026",
                                           "https://git.example/studio/acme-site"], text
        assert yaml.safe_load(text.split("---")[1])["locations"][0] == shown + "/clients/acme/website-2026"


EMAIL = re.compile(r"[\w.+-]+@([\w-]+(?:\.[\w-]+)+)")
URL = re.compile(r"https?://([^/\s)]+)")
PHONE = re.compile(r"\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}")


def kit_texts():
    for path in sorted(KIT.rglob("*")):
        if path.is_file() and path.suffix in {".md", ".yaml"}:
            yield path, path.read_text(encoding="utf-8")


def test_kit_uses_only_reserved_fictional_contact_details():
    checked = 0
    for path, text in kit_texts():
        for host in EMAIL.findall(text) + URL.findall(text):
            assert host.endswith(".example"), f"{path}: {host}"
            checked += 1
        assert not PHONE.search(text), f"{path}: phone-shaped text"
    assert checked >= 10


def test_kit_has_no_secret_shaped_strings():
    for path, text in kit_texts():
        report_text = text.lower()
        for marker in ("password:", "api_key", "token:", "-----begin"):
            assert marker not in report_text, f"{path}: {marker}"
