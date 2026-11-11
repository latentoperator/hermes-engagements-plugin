"""Copy the fictional Engagements demo kit to a folder you can point Hermes at.

Usage (from the repository root):

    python demo/setup.py                      # creates ~/engagements-demo
    python demo/setup.py --dest ~/somewhere   # a different folder
    python demo/setup.py --reset              # delete a previous demo copy and everything in it, then recreate it
    python demo/setup.py --remove             # delete a previous demo copy and everything in it

The kit is fictional data only: three clients with their project folders, and the
kickoff notes for a fourth client (Harbor Print Shop) to add live. The script uses
the standard library only and writes nothing outside the destination folder.

Before it creates, resets or removes anything it refuses:

- a filesystem root, the home folder or any folder that contains it;
- a Hermes home, anything inside one or any folder that contains one;
- this repository, anything inside it or any folder that contains it;
- a destination that is itself a symbolic link or junction;
- a non-empty folder without this script's marker, a marker that is a link, or a
  marker written for a different folder.

``--reset`` and ``--remove`` delete the whole demo folder, including anything you
added to it after setup. The marker records which folder the script created, not
which files.
"""

import argparse
import contextlib
import json
import os
import shlex
import shutil
import stat
import sys
import uuid
from pathlib import Path

KIT = Path(__file__).resolve().parent
REPO = KIT.parent
DEFAULT_DEST = "~/engagements-demo"
MARKER = ".engagements-demo"
MARKER_HEADER = "Created by hermes-engagements demo/setup.py. Safe to delete.\n"
PARTS = ("library", "clients", "paste")


class Refused(Exception):
    """A destination the script will not create, reset or remove."""


def _key(path):
    """A comparable form of an already resolved path (case-folded where the OS folds case)."""
    return Path(os.path.normcase(str(path)))


def _inside_or_equal(path, other):
    return path == other or path.is_relative_to(other)


def _real(path):
    return Path(os.path.realpath(path))


def hermes_homes():
    """Every Hermes home this user plausibly has, resolved: HERMES_HOME and the platform defaults."""
    homes = [Path.home() / ".hermes"]
    if os.environ.get("HERMES_HOME", "").strip():
        homes.append(Path(os.path.expanduser(os.path.expandvars(os.environ["HERMES_HOME"]))))
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "").strip()
        homes.append((Path(local) if local else Path.home() / "AppData" / "Local") / "hermes")
    return [_real(h) for h in homes]


def check_destination(dest):
    """Refuse a dangerous destination before anything is written or deleted. Returns the resolved path."""
    real = _real(dest)
    # The last component must not be an alias (a symlink, or a junction on Windows): resolving
    # only the parent and appending the name must reach the same place as resolving the whole path.
    if dest.is_symlink() or _key(real) != _key(_real(dest.parent) / dest.name):
        raise Refused(f"Refusing to use {dest}: it is a link to another folder. Pass the real folder instead.")
    if dest.exists() and not dest.is_dir():
        raise Refused(f"Refusing to use {dest}: it is a file, not a folder.")
    key = _key(real)
    if key.parent == key:
        raise Refused(f"Refusing to use {dest}: it is a filesystem root.")
    if _inside_or_equal(_key(_real(Path.home())), key):
        raise Refused(f"Refusing to use {dest}: it is your home folder or contains it. "
                      f"Pick a new folder such as {DEFAULT_DEST}.")
    for hermes in hermes_homes():
        h = _key(hermes)
        if _inside_or_equal(key, h) or _inside_or_equal(h, key):
            raise Refused(f"Refusing to use {dest}: it is, contains or is inside the Hermes home {hermes}.")
    repo = _key(_real(REPO))
    if _inside_or_equal(key, repo) or _inside_or_equal(repo, key):
        raise Refused(f"Refusing to use {dest}: it is, contains or is inside this repository ({REPO}).")
    return real


def marker_text(real):
    return MARKER_HEADER + f"folder: {real}\n"


def check_marker(dest, real):
    """The folder is ours only if it holds a regular-file marker written for exactly this folder."""
    marker = dest / MARKER
    try:
        info = marker.lstat()
    except FileNotFoundError:
        raise Refused(f"Refusing to touch {dest}: it is not empty and was not created by this script.") from None
    if not stat.S_ISREG(info.st_mode):
        raise Refused(f"Refusing to touch {dest}: its {MARKER} marker is not a regular file.")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise Refused(f"Refusing to touch {dest}: its {MARKER} marker belongs to another user.")
    text = None
    if info.st_size <= 4096:
        with contextlib.suppress(OSError, UnicodeDecodeError):
            text = marker.read_text(encoding="utf-8")
    if text != marker_text(real):
        raise Refused(f"Refusing to touch {dest}: its {MARKER} marker was not written for this folder. "
                      "If you moved or copied a demo folder, delete it yourself.")


def check_text(value):
    if any(ord(c) < 32 or ord(c) == 127 or c in "\x85\u2028\u2029" for c in value):
        raise Refused("Refusing a destination with control characters or line breaks in its name.")


def display(path):
    """Show ``path`` with ``~`` for the home folder, the way records write locations."""
    home = Path.home().resolve()
    try:
        return "~/" + path.resolve().relative_to(home).as_posix()
    except ValueError:
        return str(path.resolve())


def prepare(dest, reset):
    real = check_destination(dest)
    if dest.exists() and any(dest.iterdir()):
        check_marker(dest, real)
        if not reset:
            raise Refused(f"{dest} already holds a demo copy. Run again with --reset to start over.")
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)
    # Marked before copying, so an interrupted copy can still be reset or removed.
    (dest / MARKER).write_text(marker_text(real), encoding="utf-8", newline="\n")


def remove(dest):
    real = check_destination(dest)
    if not dest.exists():
        print(f"Nothing to remove: {dest} does not exist.")
        return 0
    check_marker(dest, real)
    shown = display(dest)
    shutil.rmtree(dest)
    print(f"Removed {shown}")
    return 0


def yaml_string(value):
    """A YAML double-quoted scalar. JSON string syntax is a subset of YAML's, backslashes included."""
    return json.dumps(value, ensure_ascii=False)


def rewrite_location_line(line, shown):
    """Rewrite one shipped ``locations: [a, b]`` line, quoting each complete rewritten entry."""
    head, _, rest = line.partition("[")
    body, _, tail = rest.rpartition("]")
    items = []
    for item in body.split(", "):  # the shipped entries are plain and never contain ", "
        if item.startswith(DEFAULT_DEST):
            item = yaml_string(shown + item[len(DEFAULT_DEST):])
        items.append(item)
    return f"{head}[{', '.join(items)}]{tail}"


def rewrite_text(text, shown):
    """Front matter locations become quoted YAML strings; body text is Markdown and is replaced as is."""
    lines = text.split("\n")
    frontmatter = bool(lines) and lines[0] == "---"
    for index, line in enumerate(lines):
        if frontmatter and index > 0 and line == "---":
            frontmatter = False
        elif frontmatter and DEFAULT_DEST in line:
            if not (line.startswith("locations: [") and line.endswith("]")):
                raise RuntimeError(f"demo kit: unexpected demo path in front matter: {line!r}")
            lines[index] = rewrite_location_line(line, shown)
        else:
            lines[index] = line.replace(DEFAULT_DEST, shown)
    return "\n".join(lines)


def rewrite_locations(dest, shown):
    """Point every record and note at ``dest`` when it is not the default folder."""
    if shown == DEFAULT_DEST:
        return
    for path in list((dest / "library").rglob("*.md")) + list((dest / "paste").rglob("*.md")):
        text = path.read_text(encoding="utf-8")
        if DEFAULT_DEST in text:
            path.write_text(rewrite_text(text, shown), encoding="utf-8", newline="\n")


# PowerShell ends a single-quoted string at any of these; doubling one gives a literal one.
POWERSHELL_QUOTES = "'\u2018\u2019\u201a\u201b"


def powershell_literal(text):
    """``text`` as a PowerShell single-quoted string, where nothing is expanded."""
    return "'" + "".join(c * 2 if c in POWERSHELL_QUOTES else c for c in text) + "'"


def shell_arg(shown, real, windows=None):
    """The library path as one argument: PowerShell on Windows, a POSIX shell elsewhere."""
    if windows is None:
        windows = sys.platform == "win32"
    if windows:
        text = str(real)
        return text if all(c.isascii() and (c.isalnum() or c in "_.:\\/-") for c in text) \
            else powershell_literal(text)
    if shown.startswith("~/") and shlex.quote(shown[2:]) == shown[2:]:
        return shown  # left unquoted so the shell still expands ~
    return shlex.quote(str(real))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Copy the fictional Engagements demo kit to a folder.")
    parser.add_argument("--dest", default=DEFAULT_DEST, help=f"target folder (default {DEFAULT_DEST})")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--reset", action="store_true",
                         help="delete a previous demo copy, including anything added to it, and recreate it")
    actions.add_argument("--remove", action="store_true",
                         help="delete a previous demo copy, including anything added to it, and stop")
    args = parser.parse_args(argv)

    dest = Path(os.path.expanduser(args.dest)).absolute()
    try:
        check_text(str(dest))
        if args.remove:
            return remove(dest)
        prepare(dest, args.reset)
    except Refused as refused:
        print(refused, file=sys.stderr)
        return 1
    for part in PARTS:
        shutil.copytree(KIT / part, dest / part)
    # The kit ships no .engagements/ (it is gitignored, so a fresh clone would not have it).
    # Write the library metadata here, with a fresh id per copy so two demo copies are
    # never mistaken for one library.
    meta = dest / "library" / ".engagements"
    meta.mkdir(parents=True, exist_ok=True)
    (meta / "library.yaml").write_text(
        f"format: 1\nlibrary_id: {uuid.uuid4()}\n", encoding="utf-8", newline="\n")
    shown = display(dest)
    rewrite_locations(dest, shown)

    print(f"Demo kit ready in {shown}")
    print(f"  library:  {shown}/library")
    print(f"  folders:  {shown}/clients")
    print(f"  paste-in: {shown}/paste/harbor-kickoff-notes.md")
    print()
    shell = "PowerShell" if sys.platform == "win32" else "your shell"
    print(f"Point the demo profile at it from {shell} (see demo/README.md):")
    library = shell_arg(f"{shown}/library", _real(dest) / "library")
    print(f"  hermes -p engagements-demo engagements init --root {library}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
