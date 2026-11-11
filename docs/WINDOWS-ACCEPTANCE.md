# Windows attended acceptance checklist

Native CI on `windows-latest` is the gate for Windows record writes. This short checklist is for a real Windows
machine (for example a laptop), run attended, after CI is green. It adds what CI cannot: a person watching a
real desktop session, and the probes in section 4. Fictional data only. Nothing here touches a real client library.

## 1. Prerequisites

- Windows 10 or 11, a normal (non-admin) user, NTFS on the drive you test on.
- Python 3.11 or newer on `PATH`: `py -3 --version`.
- `uv`: `winget install --id astral-sh.uv`, then `uv --version`.
- Git for Windows, or a git bundle (below).

## 2. Get the code

Pick one:

- Clone: `git clone https://github.com/latentoperator/hermes-engagements-plugin.git`, then `git checkout <branch-or-tag>`.
  Sign in through the browser prompt (Git Credential Manager). Do not paste tokens into a shell history.
- Bundle: on the build host run `git bundle create engagements.bundle --all`, copy the file over (USB or a
  share), then `git clone engagements.bundle hermes-engagements` and `git checkout <branch-or-tag>`.

Keep the checkout on a local NTFS path, not a OneDrive-synced folder, for this test.

## 3. Run the suite

```powershell
cd hermes-engagements
uv run --no-project --python 3.14 --with pytest --with "pyyaml>=6,<7" python -m pytest -q -p no:cacheprovider
uv run --no-project --python 3.11 --with pytest --with "pyyaml>=6,<7" python -m pytest -q -p no:cacheprovider
```

Expected: no failures. Skipped tests are the POSIX-only ones listed in the pull request. Record the final line
of each run (`N passed, M skipped`) in the pull request.

## 3a. What runs on Windows in CI, and where

The suites below are the Windows write-path evidence. All of it is pytest on `windows-latest` in GitHub
Actions. None of it was run on a physical Windows machine, and nothing below is a Desktop or TUI session.

| Suite | Where it runs | What it covers |
| --- | --- | --- |
| `tests/test_windows_native.py` | `windows-latest` only | handle-confined writes; a reparse point at any component is refused |
| `tests/test_windows_handle_ownership.py` | everywhere against a fake backend, and `windows-latest` against the native one | a failed check closes the handle it acquired, exactly once |
| `tests/test_windows_write_path.py` | everywhere; the native backend on `windows-latest`, a protocol adapter over POSIX descriptors elsewhere | a check that fails after the rename is reported as a committed write with a `maintenance_warning`; a create that fails its own validation leaves no temp file behind; a failure before the rename is still an error and leaves the old bytes |
| `tests/test_binding_paths.py` | everywhere; the junction case on `windows-latest` | a drive letter or junction that resolves to a UNC path is not a local folder and does not match, while a junction to a local folder still does |

The protocol adapter is a test double for the `_winfs` surface only: it runs the real `_WinDir`,
`writer` and `tools` code against real files, and it is what makes the same test meaningful on Ubuntu.
The `windows-latest` jobs are what prove the native handle backend behaves the same way.

## 4. Manual probes

Run this setup block in **every** PowerShell window you open for these probes (4b and 4c use two windows).
Every snippet below reads the paths from the environment, so no literal `$lib` ends up inside Python.

```powershell
$lib = Join-Path $env:TEMP "eng-lib"
$out = Join-Path $env:TEMP "eng-outside"
New-Item -ItemType Directory -Force -Path $lib, $out | Out-Null
$env:ENG_LIB = $lib
$env:ENG_OUTSIDE = $out
```

Snippets are piped to Python, run from the repository root:

```powershell
@'
import os
from datetime import datetime, timezone

from engagements.library import open_library

lib = open_library(os.environ["ENG_LIB"], create_default=True)
lib.create("client", "Acme Bakery", profile="windows-acceptance", today=datetime.now(timezone.utc))
print("library:", os.environ["ENG_LIB"])
'@ | uv run --no-project --python 3.14 --with "pyyaml>=6,<7" python -
```

### 4a. Junction swap

**Step 1.** Move the real client directory aside, then put a junction to the outside folder in its place
(`mklink /J` refuses a name that already exists):

```powershell
Rename-Item "$lib\Acme Bakery" "Acme Bakery.real"
cmd /c mklink /J "$lib\Acme Bakery" $out
```

**Step 2.** Ask the plugin to create a project under that client:

```powershell
@'
import os
from datetime import datetime, timezone

from engagements.errors import EngagementsError
from engagements.library import open_library

lib = open_library(os.environ["ENG_LIB"], create_default=False)
try:
    lib.create("project", "Menu Refresh", client="acme-bakery", profile="windows-acceptance",
               today=datetime.now(timezone.utc))
except EngagementsError as exc:
    print("refused:", exc.code, exc.details)
else:
    print("UNEXPECTED: the create went through the junction")
'@ | uv run --no-project --python 3.14 --with "pyyaml>=6,<7" python -
```

Expected: a controlled `io_error` (or `not_found` / `outside_root`) naming a relative path; `$out` stays empty
(`Get-ChildItem $out` prints nothing); no Python traceback and no absolute path in the message.

Remove the junction and restore the client when done:
`cmd /c rmdir "$lib\Acme Bakery"`, then `Rename-Item "$lib\Acme Bakery.real" "Acme Bakery"`.

### 4b. A held directory cannot be renamed mid-write

**Step 1.** Window A holds the client directory and waits for you. The script goes to a file, not a pipe,
because a piped script has already consumed standard input and `input()` would return at once:

```powershell
$hold = Join-Path $env:TEMP "eng-hold.py"
@'
import os

from engagements.fsutil import _Dir

with _Dir.open_root(os.path.join(os.environ["ENG_LIB"], "Acme Bakery")) as held:
    print("HELD", held.relpath or ".", flush=True)
    input("Press Enter to release...")
'@ | Set-Content -Encoding utf8 $hold
uv run --no-project --python 3.14 --with "pyyaml>=6,<7" python $hold
```

**Step 2.** Window B (run the setup block first), while A is still holding:
`Rename-Item "$lib\Acme Bakery" "Moved Acme"`.

Expected: B fails with "The process cannot access the file because it is being used by another process"
(a `PermissionError` in Python terms). Press Enter in A, then run the same rename again: it succeeds. Rename it
back afterwards.

### 4c. Lock contention

A record update takes the record's own lock, `.engagements\locks\<id>.lock`, so both windows contend on
`acme-bakery.lock`, the lock an update of the Acme Bakery client takes.

**Step 1.** Window A holds that lock for 20 seconds:

```powershell
$lockhold = Join-Path $env:TEMP "eng-lock.py"
@'
import os
import sys
import time

from engagements.fsutil import FileLock

path = os.path.join(os.environ["ENG_LIB"], ".engagements", "locks", "acme-bakery.lock")
with FileLock(path, timeout=float(sys.argv[1])):
    print("LOCKED", flush=True)
    time.sleep(float(sys.argv[2]))
print("RELEASED", flush=True)
'@ | Set-Content -Encoding utf8 $lockhold
uv run --no-project --python 3.14 --with "pyyaml>=6,<7" python $lockhold 5 20
```

**Step 2.** Window B (run the setup block, then `$lockhold = Join-Path $env:TEMP "eng-lock.py"`), within those
20 seconds:
`uv run --no-project --python 3.14 --with "pyyaml>=6,<7" python $lockhold 10 0`. An `engagements_update` on
`acme-bakery` with the default timeout contends on the same lock and should behave the same way.

Expected: B fails with `LockTimeout` (an update returns `lock_timeout`) after about 10 seconds and the record is
unchanged. Run B again after A prints `RELEASED`: it prints `LOCKED` and `RELEASED`. Run `Get-Process python` afterwards: nothing is left holding the lock file.

### 4d. Create never overwrites

Create two clients with the same name and `allow_similar=True`:

```powershell
@'
import os
from datetime import datetime, timezone

from engagements.library import open_library

lib = open_library(os.environ["ENG_LIB"], create_default=False)
now = datetime.now(timezone.utc)
first = lib.create("client", "Riverbend Dental", profile="windows-acceptance", today=now)
second = lib.create("client", "Riverbend Dental", allow_similar=True, profile="windows-acceptance", today=now)
print("first:", first.path)
print("second:", second.path)
'@ | uv run --no-project --python 3.14 --with "pyyaml>=6,<7" python -
```

Expected: the second path carries ` (2)` and the first record's bytes are unchanged (`Get-FileHash` the first
path before and after).

## 5. Report

Record: Windows build (`winver`), Python versions, the two pytest summary lines, and a pass/fail with one line
for each of 4a to 4d. Anything unexpected goes in the pull request verbatim, with the exact command that produced it.
