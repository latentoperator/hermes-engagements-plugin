"""Record locks: thread and process contention, a fresh FileLock per acquisition."""
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from engagements import writer
from engagements.errors import EngagementsError
from engagements.fsutil import FileLock
from engagements.records import parse
from writer_support import do_update, op_comm_append, op_status

ROOT = Path(__file__).resolve().parent.parent

# A raw lock holder, so the test shows FileLock excludes another process using the platform's own lock call.
HOLDER = """
import sys, time
handle = open(sys.argv[1], 'a+b')
if sys.platform == 'win32':
    import msvcrt
    handle.seek(0)
    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
else:
    import fcntl
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
print('locked', flush=True)
time.sleep(float(sys.argv[2]))
"""

UPDATER = """
import sys, time
sys.path.insert(0, sys.argv[1])
sys.path.insert(0, sys.argv[1] + '/tests')
from pathlib import Path
from engagements import writer
from writer_support import fixed_clock, client_exists

root, path, base, hold = Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4], float(sys.argv[5])

def hold_the_lock():
    print('in-critical-section', flush=True)
    time.sleep(hold)

result = writer.update(root, path, base, 'process one', [{'op': 'append_to_section', 'section': 'Summary', 'text': 'from process one'}],
                       kind='client', profile='studio-ops', clock=fixed_clock, client_exists=client_exists,
                       before_final_rehash=hold_the_lock)
print('done', result.data['rebased'], flush=True)
"""


def lock_file(lib):
    return lib.meta / "locks" / "acme-bakery.lock"


def test_the_lock_is_the_record_lock_file(lib):
    do_update(lib, lib.base(), [op_status()])
    assert lock_file(lib).is_file()


def test_each_acquisition_uses_a_fresh_file_lock(lib, monkeypatch):
    created = []

    class Recording(FileLock):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created.append(self)

    monkeypatch.setattr(writer, "FileLock", Recording)
    base = lib.base()
    first = do_update(lib, base, [op_status("paused")]).data["new_rev"]
    do_update(lib, first, [op_status("active")])
    assert len(created) == 2 and created[0] is not created[1]
    assert all(lock._file is None for lock in created)


def test_two_threads_in_one_process_exclude_each_other(lib):
    base = lib.base()
    inside = threading.Event()
    release = threading.Event()
    finished = {}

    def first():
        finished["a"] = do_update(lib, base, [op_comm_append("- from thread a")],
                                  before_final_rehash=lambda: (inside.set(), release.wait(5)))

    def second():
        inside.wait(5)
        finished["b"] = do_update(lib, base, [{"op": "append_to_section", "section": "Summary", "text": "from thread b"}],
                                  profile="studio-ops")

    one, two = threading.Thread(target=first), threading.Thread(target=second)
    one.start()
    two.start()
    assert inside.wait(5)
    time.sleep(0.5)
    assert "b" not in finished  # thread b is still waiting on the record lock
    assert two.is_alive()
    release.set()
    one.join(10)
    two.join(10)
    assert finished["a"].data["rebased"] is False
    assert finished["b"].data["rebased"] is True  # b held the old rev; a's disjoint edit is rebased over
    text = lib.read().decode()
    assert "- from thread a" in text and "from thread b" in text
    assert "studio-dev: append to Communication preferences" in text
    assert "studio-ops: append to Summary" in text


def test_many_threads_never_lose_an_update(lib):
    base = lib.base()
    sections = ["Summary", "Current work", "Locations", "Systems of record", "History", "Unconfirmed", "Contacts",
                "Communication preferences"]
    errors = []

    def work(section):
        try:
            do_update(lib, base, [{"op": "append_to_section", "section": section, "text": f"- note for {section}"}],
                      profile=f"p-{section[:3]}")
        except Exception as exc:  # noqa: BLE001 - collected and asserted below
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(section,)) for section in sections]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert errors == []
    text = lib.read().decode()
    for section in sections:
        assert f"- note for {section}" in text
    assert text.count(": append to ") == len(sections)  # every Change log line survived
    assert len(lib.history()) == len(sections)
    assert parse(lib.read()).defects == set()


def test_lock_timeout_when_another_holder_never_lets_go(lib):
    base = lib.base()
    (lib.meta / "locks").mkdir()
    before = lib.read()
    with FileLock(lock_file(lib), timeout=1):
        started = time.monotonic()
        with pytest.raises(EngagementsError) as exc:
            do_update(lib, base, [op_status()], lock_timeout=0.3)
        assert time.monotonic() - started < 3
    assert exc.value.code == "lock_timeout"
    assert str(lib.root) not in str(exc.value.details)
    assert lib.read() == before and lib.leftovers() == []


def test_the_default_lock_timeout_is_ten_seconds():
    assert writer.LOCK_TIMEOUT == 10.0


def test_a_process_holding_the_lock_blocks_us_then_releases(lib):
    base = lib.base()
    (lib.meta / "locks").mkdir()
    holder = subprocess.Popen([sys.executable, "-I", "-c", HOLDER, str(lock_file(lib)), "1.0"],
                              stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "locked"
        with pytest.raises(EngagementsError) as exc:
            do_update(lib, base, [op_status()], lock_timeout=0.3)
        assert exc.value.code == "lock_timeout"
        started = time.monotonic()
        result = do_update(lib, base, [op_status("paused")], lock_timeout=10)  # the loser waits
        assert time.monotonic() - started > 0.2
        assert result.data["rebased"] is False
    finally:
        holder.wait(10)


def test_two_processes_updating_the_same_record_contend(lib):
    base = lib.base()
    first = subprocess.Popen(
        [sys.executable, "-c", UPDATER, str(ROOT), str(lib.root), str(lib.path), base, "1.5"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert first.stdout.readline().strip() == "in-critical-section"
        with pytest.raises(EngagementsError) as exc:
            do_update(lib, base, [op_comm_append()], lock_timeout=0.3)
        assert exc.value.code == "lock_timeout"
        started = time.monotonic()
        waiter = do_update(lib, base, [op_comm_append()], profile="studio-dev", lock_timeout=10)
        assert time.monotonic() - started > 0.3  # it waited for the other process
        assert waiter.data["rebased"] is True
        out, err = first.communicate(timeout=10)
        assert first.returncode == 0, err
        assert out.strip().endswith("done False")
    finally:
        if first.poll() is None:
            first.kill()
    text = lib.read().decode()
    assert "from process one" in text and "- Monthly review call" in text
    assert "studio-ops: append to Summary" in text and "studio-dev: append to Communication preferences" in text
    assert len(lib.history()) == 2
